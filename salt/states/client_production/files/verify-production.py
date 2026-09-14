#!/usr/bin/env python3
"""Verify a published client without creating resources or writing local state.

Uses the installed production helper, private config and hardened receipt.
HTTPS certificate verification is mandatory; requests never follow redirects.
The only remote mutations are the requested Odoo login and immediate logout.
Output contains named booleans/status codes only, never response bodies.
"""

import base64
import json
import os
import re
import stat
import types
from pathlib import Path

import requests
import tomllib

HELPER = Path("/usr/local/libexec/oduflow-create-production")
CONFIG = Path("/etc/oduflow/production.json")
RECEIPT = Path("/var/lib/oduflow/production/receipt.json")
PASEO_ENV = Path("/etc/paseo/credentials.env")
LIMIT = 1024 * 1024


class CheckFailed(Exception):
    pass


def load_helper():
    info = HELPER.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise CheckFailed()
    module = types.ModuleType("installed_production_helper")
    module.__file__ = str(HELPER)
    # Compile directly: SourceFileLoader could write a __pycache__ artifact.
    exec(compile(HELPER.read_bytes(), str(HELPER), "exec"), module.__dict__)
    return module


def session():
    result = requests.Session()
    result.trust_env = False  # No inherited proxies/netrc can redirect credentials.
    return result


def request(client, method, url, **kwargs):
    response = client.request(
        method, url, timeout=(5, 30), allow_redirects=False, verify=True, stream=True, **kwargs
    )
    try:
        content = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            content.extend(chunk)
            if len(content) > LIMIT:
                raise CheckFailed()
        try:
            value = json.loads(content) if content else None
        except (ValueError, UnicodeError):
            value = None
        return response.status_code, value
    finally:
        response.close()


def rpc(client, url, method, params):
    return request(
        client,
        "POST",
        url + "/web/session/" + method,
        json={"jsonrpc": "2.0", "method": "call", "id": 1, "params": params},
    )


def check_odoo(config, info, password, checks, factory):
    client = factory()
    url = "https://" + config["domain"]
    try:
        code, body = rpc(
            client,
            url,
            "authenticate",
            {
                "db": info["db_name"],
                "login": config.get("admin_login", "admin"),
                "password": password,
            },
        )
        checks["odoo_login_http_status"] = code
        result = body.get("result") if isinstance(body, dict) and not body.get("error") else None
        valid_login = (
            isinstance(result, dict) and type(result.get("uid")) is int and result["uid"] > 0
        )
        checks["odoo_admin_login"] = bool(valid_login)
        version = result.get("server_version", "") if valid_login else ""
        version_info = result.get("server_version_info", []) if valid_login else []
        checks["odoo_version_19"] = bool(
            code == 200
            and valid_login
            and (
                (isinstance(version, str) and re.match(r"^19\.\d+(?:\D|$)", version))
                or (
                    isinstance(version_info, list)
                    and version_info
                    and type(version_info[0]) is int
                    and version_info[0] == 19
                )
            )
        )
        if code != 200:
            checks["odoo_admin_login"] = False
    except Exception:
        checks["odoo_admin_login"] = False
    finally:
        try:
            code, body = rpc(client, url, "destroy", {})
            checks["odoo_logout_http_status"] = code
            # Odoo 19 omits result for controller methods returning None.
            # Accept that envelope, but prove the session is no longer usable.
            accepted = bool(
                (code == 204 and body is None)
                or (
                    code == 200
                    and isinstance(body, dict)
                    and body.get("jsonrpc") == "2.0"
                    and body.get("id") == 1
                    and not body.get("error")
                )
            )
            code, body = rpc(client, url, "check", {})
            checks["odoo_session_check_http_status"] = code
            error = body.get("error") if isinstance(body, dict) else None
            details = error.get("data") if isinstance(error, dict) else None
            checks["odoo_logout"] = bool(
                accepted
                and code == 200
                and isinstance(details, dict)
                and details.get("name") == "odoo.http.SessionExpiredException"
            )
        except Exception:
            checks["odoo_logout"] = False
        client.close()


def protected_endpoint(factory, url, authorization, prefix, checks, verify=None):
    client = factory()
    try:
        code, _ = request(client, "GET", url)
        checks[prefix + "_anonymous_http_status"] = code
        checks[prefix + "_anonymous_denied"] = code == 401
        code, body = request(client, "GET", url, headers={"Authorization": authorization})
        checks[prefix + "_authenticated_http_status"] = code
        authenticated = code == 200
        if authenticated and verify is not None:
            verify(body)
        checks[prefix + "_authenticated"] = authenticated
    except Exception:
        checks[prefix + "_authenticated"] = False
    finally:
        client.close()


def verify(helper, config_path=CONFIG, receipt_path=RECEIPT, factory=session):
    checks = {
        name: False
        for name in (
            "local_hardened_identity",
            "odoo_admin_login",
            "odoo_version_19",
            "odoo_logout",
            "oduflow_anonymous_denied",
            "oduflow_authenticated",
            "paseo_anonymous_denied",
            "paseo_authenticated",
        )
    }
    try:
        config = helper.validate(helper.private_json(config_path))
        helper.verify_runtime_config(config)
        receipt = helper.private_json(receipt_path)
        container_id = receipt.get("container_id")
        if (
            receipt.get("status") != "hardened"
            or receipt.get("fingerprint") != helper.fingerprint(config)
            or not isinstance(container_id, str)
            or not re.fullmatch(r"[a-f0-9]{64}", container_id)
        ):
            raise CheckFailed()
        info = helper.LocalAPI(config).info()
        if (
            not isinstance(info, dict)
            or info.get("container_status") != "running"
            or info.get("deploy_in_progress")
        ):
            raise CheckFailed()
        helper.verify_info(info, config)
        helper.verify_container(info, config, container_id)
        checks["local_hardened_identity"] = True
        runtime = tomllib.loads(helper.private_read(helper.ODUFLOW_CONFIG))
        paseo_host = runtime["route"]["paseo"]["host"]
        if paseo_host != "paseo." + config["domain"]:
            raise CheckFailed()
        paseo_env = helper.private_read(PASEO_ENV)
        credentials = {}
        for line in paseo_env.splitlines():
            match = re.fullmatch(
                r"(PASEO_PASSWORD|ODUFLOW_MCP_TOKEN)=([A-Za-z0-9_-]{16,4096})", line
            )
            if not match or match[1] in credentials:
                raise CheckFailed()
            credentials[match[1]] = match[2]
        paseo_password = credentials["PASEO_PASSWORD"]
        password = helper.private_read(config["admin_password_file"]).rstrip("\n")
        ui_password = helper.private_read(config["ui_password_file"]).rstrip("\n")
        if not password or not ui_password:
            raise CheckFailed()
    except Exception:
        return {"checks": checks, "overall": False}
    check_odoo(config, info, password, checks, factory)

    def verify_oduflow(body):
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise CheckFailed()
        helper.verify_info(body, config)

    protected_endpoint(
        factory,
        "https://" + config["team_hostname"] + "/api/productions/" + config["name"],
        "Basic " + base64.b64encode(("admin:" + ui_password).encode()).decode(),
        "oduflow",
        checks,
        verify=verify_oduflow,
    )
    protected_endpoint(
        factory,
        "https://" + paseo_host + "/api/status",
        "Bearer " + paseo_password,
        "paseo",
        checks,
    )
    return {
        "checks": checks,
        "overall": all(value for value in checks.values() if type(value) is bool),
    }


def main():
    if os.geteuid() != 0:
        print(json.dumps({"checks": {"root_required": False}, "overall": False}))
        return 1
    try:
        result = verify(load_helper())
    except Exception:
        result = {"checks": {"verification_available": False}, "overall": False}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["overall"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
