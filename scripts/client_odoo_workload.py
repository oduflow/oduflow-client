#!/usr/bin/env python3
"""Bounded authenticated read workload for an already hardened client Odoo.

Run on the expected client as root. Credentials stay in existing private files
and HTTPS request bodies; output contains aggregate timings and error categories
only. Each virtual user has an independent session using the existing admin
account. Login/logout may update authentication metadata, but no users or
business records are created or changed. This is a synthetic small-database
smoke test, not a production capacity estimate.
"""

import argparse
import concurrent.futures
import json
import math
import os
import re
import stat
import threading
import time
import types
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import requests

HELPER = Path("/usr/local/libexec/oduflow-create-production")
CONFIG = Path("/etc/oduflow/production.json")
RECEIPT = Path("/var/lib/oduflow/production/receipt.json")
LIMIT = 1024 * 1024


class WorkloadError(Exception):
    """Only fixed, non-sensitive categories may be included in reports."""


def database_summary(helper, container_id, database):
    """Read only aggregate database size/counts through the verified container."""
    code = (
        "import json,os,sys,psycopg2; "
        "connection=psycopg2.connect(dbname=sys.argv[1],host=os.environ['HOST'],"
        "user=os.environ['USER'],password=os.environ['PASSWORD'],connect_timeout=5,"
        "options='-c statement_timeout=10000'); "
        "connection.set_session(readonly=True,autocommit=True); "
        "cursor=connection.cursor(); "
        'cursor.execute("SELECT pg_database_size(current_database()), '
        "(SELECT count(*) FROM res_partner), (SELECT count(*) FROM res_users), "
        "(SELECT count(*) FROM ir_module_module WHERE state='installed')\"); "
        "print(json.dumps(dict(zip(['size_bytes','partner_rows','user_rows',"
        "'installed_modules'],cursor.fetchone())))); connection.close()"
    )
    try:
        raw = helper.docker_run(
            ["exec", "--user", "odoo", container_id, "python3", "-c", code, database]
        )
        result = json.loads(raw)
        expected = {"size_bytes", "partner_rows", "user_rows", "installed_modules"}
        if not isinstance(result, dict) or set(result) != expected:
            raise ValueError
        if any(type(value) is not int or value < 0 for value in result.values()):
            raise ValueError
        return result
    except Exception:
        return {"error": "database_summary_unavailable"}


def load_identity(expected_uuid):
    if os.geteuid() != 0:
        raise WorkloadError("root_required")
    try:
        if str(UUID(expected_uuid)) != expected_uuid:
            raise ValueError
        metadata = HELPER.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise ValueError
        helper = types.ModuleType("installed_production_helper")
        helper.__file__ = str(HELPER)
        exec(compile(HELPER.read_bytes(), str(HELPER), "exec"), helper.__dict__)
        config = helper.validate(helper.private_json(CONFIG))
        if config["instance_uuid"] != expected_uuid:
            raise ValueError
        helper.verify_runtime_config(config)
        receipt = helper.private_json(RECEIPT)
        container_id = receipt.get("container_id")
        if (
            receipt.get("status") != "hardened"
            or receipt.get("fingerprint") != helper.fingerprint(config)
            or not isinstance(container_id, str)
            or not re.fullmatch(r"[a-f0-9]{64}", container_id)
        ):
            raise ValueError
        info = helper.LocalAPI(config).info()
        if (
            not isinstance(info, dict)
            or info.get("container_status") != "running"
            or info.get("deploy_in_progress")
        ):
            raise ValueError
        helper.verify_info(info, config)
        if helper.verify_container(info, config, container_id) != container_id:
            raise ValueError
        password = helper.private_read(config["admin_password_file"]).rstrip("\n")
        if not password:
            raise ValueError
        return {
            "origin": "https://" + config["domain"],
            "database": info["db_name"],
            "login": config.get("admin_login", "admin"),
            "password": password,
            "database_summary": database_summary(helper, container_id, info["db_name"]),
        }
    except Exception:
        raise WorkloadError("client_identity_or_credentials_unverified") from None


def rpc(session, origin, path, params, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WorkloadError("phase_time_budget")
    try:
        with session.post(
            origin + path,
            json={"jsonrpc": "2.0", "method": "call", "id": 1, "params": params},
            timeout=(min(5, remaining), min(15, remaining)),
            allow_redirects=False,
            verify=True,
            stream=True,
        ) as response:
            if response.status_code != 200:
                raise WorkloadError("http_" + str(response.status_code))
            content = bytearray()
            for chunk in response.iter_content(4096):
                content.extend(chunk)
                if len(content) > LIMIT:
                    raise WorkloadError("response_too_large")
                if time.monotonic() > deadline:
                    raise WorkloadError("phase_time_budget")
            result = json.loads(content)
            if not isinstance(result, dict) or result.get("jsonrpc") != "2.0":
                raise WorkloadError("invalid_rpc_envelope")
            if result.get("error"):
                raise WorkloadError("rpc_error")
            return result.get("result")
    except requests.Timeout:
        raise WorkloadError("request_timeout") from None
    except requests.RequestException:
        raise WorkloadError("request_transport") from None
    except (ValueError, UnicodeError):
        raise WorkloadError("invalid_json") from None


def is_version_19(result):
    if not isinstance(result, dict):
        return False
    version = result.get("server_version_info")
    return bool(
        isinstance(version, list) and version and type(version[0]) is int and version[0] == 19
    )


def sample(events, name, call, validate):
    start = time.monotonic()
    failure = None
    value = None
    try:
        value = call()
        if not validate(value):
            raise WorkloadError("unexpected_result")
    except WorkloadError as error:
        failure = str(error)
    except Exception:
        failure = "local_check_failed"
    events.append({"operation": name, "ms": (time.monotonic() - start) * 1000, "error": failure})
    return value if failure is None else None


def virtual_user(identity, iterations, barrier, deadline):
    events, counts = [], []
    client = requests.Session()
    client.trust_env = False
    origin = identity["origin"]
    client.headers.update(
        {"Origin": origin, "Referer": origin + "/web", "Accept": "application/json"}
    )
    uid = None
    try:
        barrier.wait(timeout=10)
        authenticated = sample(
            events,
            "login",
            lambda: rpc(
                client,
                origin,
                "/web/session/authenticate",
                {
                    "db": identity["database"],
                    "login": identity["login"],
                    "password": identity["password"],
                },
                deadline,
            ),
            lambda value: (
                is_version_19(value) and type(value.get("uid")) is int and value["uid"] > 0
            ),
        )
        if authenticated is None:
            return events, counts
        uid = authenticated["uid"]
        for _iteration in range(iterations):
            if time.monotonic() >= deadline:
                events.append({"operation": "phase", "ms": 0, "error": "phase_time_budget"})
                break
            session_result = sample(
                events,
                "session_info",
                lambda: rpc(client, origin, "/web/session/get_session_info", {}, deadline),
                lambda value: is_version_19(value) and value.get("uid") == uid,
            )
            if session_result is None:
                break
            sample(
                events,
                "version",
                lambda: rpc(client, origin, "/web/webclient/version_info", {}, deadline),
                is_version_19,
            )
            records = sample(
                events,
                "search_read",
                lambda: rpc(
                    client,
                    origin,
                    "/web/dataset/call_kw/res.partner/search_read",
                    {
                        "model": "res.partner",
                        "method": "search_read",
                        "args": [],
                        "kwargs": {
                            "domain": [["active", "=", True]],
                            "fields": ["id"],
                            "limit": 20,
                            "order": "id asc",
                        },
                    },
                    deadline,
                ),
                lambda value: (
                    isinstance(value, list)
                    and 0 < len(value) <= 20
                    and all(isinstance(row, dict) and type(row.get("id")) is int for row in value)
                ),
            )
            if records is not None:
                counts.append(len(records))
            time.sleep(0.2)
    except Exception:
        events.append({"operation": "worker", "ms": 0, "error": "worker_failed"})
    finally:
        if uid is not None:
            sample(
                events,
                "logout",
                lambda: rpc(client, origin, "/web/session/destroy", {}, time.monotonic() + 5),
                lambda value: value is None or value is True,
            )
        client.close()
    return events, counts


def percentile(values, proportion):
    if not values:
        return None
    return round(sorted(values)[max(0, math.ceil(proportion * len(values)) - 1)], 2)


def summarize(events):
    successful = [event["ms"] for event in events if event["error"] is None]
    errors = Counter(event["error"] for event in events if event["error"] is not None)
    return {
        "requests": len(events),
        "successful": len(successful),
        "errors": dict(sorted(errors.items())),
        "p50_ms": percentile(successful, 0.50),
        "p95_ms": percentile(successful, 0.95),
        "max_ms": round(max(successful), 2) if successful else None,
    }


def run_phase(identity, concurrency, iterations):
    barrier = threading.Barrier(concurrency)
    start = time.monotonic()
    deadline = start + 120
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(virtual_user, identity, iterations, barrier, deadline)
            for _user in range(concurrency)
        ]
        results = [future.result() for future in futures]
    elapsed = time.monotonic() - start
    events = [event for result, _counts in results for event in result]
    counts = [count for _events, rows in results for count in rows]
    summary = summarize(events)
    summary.update(
        concurrent_sessions=concurrency,
        iterations_per_session=iterations,
        elapsed_seconds=round(elapsed, 3),
        successful_requests_per_second=round(summary["successful"] / elapsed, 2),
        records_per_read_min=min(counts) if counts else None,
        records_per_read_max=max(counts) if counts else None,
        operations={
            operation: summarize([event for event in events if event["operation"] == operation])
            for operation in sorted({event["operation"] for event in events})
        },
    )
    expected_reads = concurrency * iterations
    summary["completed"] = bool(
        not summary["errors"]
        and summary["operations"].get("search_read", {}).get("successful") == expected_reads
        and summary["operations"].get("logout", {}).get("successful") == concurrency
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-instance-uuid", required=True)
    parser.add_argument("--iterations", type=int, default=30, choices=range(1, 51), metavar="1..50")
    options = parser.parse_args()
    try:
        identity = load_identity(options.expected_instance_uuid)
        phases = [run_phase(identity, concurrency, options.iterations) for concurrency in (1, 3)]
        result = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "workload": "authenticated_odoo19_read_smoke",
            "odoo_major_version": 19 if all(phase["completed"] for phase in phases) else None,
            "database_summary": identity["database_summary"],
            "limits": (
                "Synthetic fresh-database reads with up to 20 partner IDs per request. "
                "Independent sessions use one administrator account. Login/logout can update "
                "authentication metadata. No business records are written. Timings include "
                "HTTPS network and ingress; 0.2-second think time per three-read cycle. "
                "Latency percentiles include successful requests only; failures are counted "
                "separately. These results do not estimate capacity for real customer data, "
                "writes, reports, custom modules, simultaneous development or sustained load."
            ),
            "phases": phases,
            "overall": all(phase["completed"] for phase in phases),
        }
    except WorkloadError as error:
        result = {"overall": False, "error": str(error)}
    except Exception:
        result = {"overall": False, "error": "workload_unavailable"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["overall"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
