#!/usr/bin/env python3
"""Fixed single-client publication workflow; never alter provider firewall rules."""

import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import time
import types
from contextlib import ExitStack
from pathlib import Path

LIBEXEC = Path("/usr/local/libexec")
PUBLICATION_CONFIG = Path("/etc/oduflow/publication.json")
VERIFY_SECONDS = 240


class SafeError(Exception):
    pass


def load(name):
    path = LIBEXEC / name
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SafeError("unsafe_helper")
    module = types.ModuleType(name.replace("-", "_"))
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


def hardened(helper, config, receipt_path):
    if not receipt_path.exists():
        return False
    receipt = helper.private_json(receipt_path)
    if receipt.get("fingerprint") != helper.fingerprint(config):
        raise SafeError("production_receipt_mismatch")
    if receipt.get("status") != "hardened":
        return False
    container_id = receipt.get("container_id")
    if not isinstance(container_id, str) or not re.fullmatch(r"[a-f0-9]{64}", container_id):
        raise SafeError("production_receipt_invalid")
    info = helper.LocalAPI(config).info()
    if (
        not isinstance(info, dict)
        or info.get("container_status") != "running"
        or info.get("deploy_in_progress")
    ):
        raise SafeError("production_not_ready")
    helper.verify_info(info, config)
    helper.verify_container(info, config, container_id)
    return True


def restart_traefik(helper):
    try:
        item = json.loads(helper.docker_run(["inspect", "oduflow-traefik"]))[0]
        labels = item.get("Config", {}).get("Labels", {})
        identifier = item.get("Id", "")
        if (
            labels.get("oduflow.managed") != "true"
            or labels.get("oduflow.system") != "true"
            or not re.fullmatch(r"[a-f0-9]{64}", identifier)
        ):
            raise SafeError("traefik_identity_mismatch")
        helper.docker_run(["restart", "--time", "10", identifier])
    except (ValueError, IndexError, TypeError):
        raise SafeError("traefik_identity_invalid") from None


def verify_public(timeout=VERIFY_SECONDS, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + min(timeout, VERIFY_SECONDS)
    while clock() < deadline:
        remaining = deadline - clock()
        try:
            result = subprocess.run(
                [sys.executable, str(LIBEXEC / "oduflow-verify-production")],
                capture_output=True,
                timeout=min(60, remaining),
                check=False,
            )
            data = json.loads(result.stdout)
            if result.returncode == 0 and data.get("overall") is True:
                return True
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(5, remaining))
    raise SafeError("public_verification_pending")


def execute(helper, guard, config, binding, *, verify=verify_public, restart=restart_traefik):
    receipt_path = helper.STATE_DIR / "receipt.json"
    publication_path = helper.STATE_DIR / "publication.json"
    identity = {**binding, "production_fingerprint": helper.fingerprint(config)}
    publication = helper.private_json(publication_path) if publication_path.exists() else None
    if publication and publication.get("identity") != identity:
        raise SafeError("publication_identity_mismatch")
    actual = guard.apply("status", binding)
    is_hardened = hardened(helper, config, receipt_path)
    # Existing verified deployments need no guard, password reset or restart.
    if (
        is_hardened
        and actual["status"] == "open"
        and (not publication or publication.get("status") == "verified")
    ):
        verify()
        return {"changed": False, "comment": "client_production_ready"}
    changed = False
    if publication is None:
        publication = {"identity": identity, "status": "closing"}
        helper.save_state(publication_path, publication)
    if not is_hardened:
        publication["status"] = "closing"
        helper.save_state(publication_path, publication)
        closed = guard.apply("close", binding)
        if (
            closed.get("status") != "closed"
            or closed.get("ipv4") is not True
            or closed.get("ipv6") is not True
        ):
            raise SafeError("public_ingress_not_closed")
        attestation = {
            "instance_uuid": config["instance_uuid"],
            "domain": config["domain"],
            "public_ingress_closed": True,
            "guard_kind": "host_raw",
            "firewall_group_id": "host-raw:" + binding["interface"],
        }
        if helper.GUARD.exists():
            existing = helper.private_json(helper.GUARD)
            if (
                existing.get("instance_uuid") != config["instance_uuid"]
                or existing.get("domain") != config["domain"]
            ):
                raise SafeError("foreign_publication_attestation")
        helper.save_state(helper.GUARD, attestation)
        publication["status"] = "closed"
        helper.save_state(publication_path, publication)
        # An existing claimed create with unknown outcome is never re-posted.
        result = helper.execute(config, helper.LocalAPI(config), receipt_path)
        if result.get("status") != "hardened" or not hardened(helper, config, receipt_path):
            raise SafeError("production_hardening_unconfirmed")
        changed = True
    # Revalidate immutable identity immediately before ANY opening, also on recovery.
    if not hardened(helper, config, receipt_path):
        raise SafeError("production_hardening_unconfirmed")
    phase = publication["status"]
    if actual["status"] != "open" or phase in ("closing", "closed", "restoring"):
        publication["status"] = "restoring"
        helper.save_state(publication_path, publication)
        guard.apply("restore", binding)
        actual = guard.apply("status", binding)
        if actual["status"] != "open":
            raise SafeError("guard_restore_unconfirmed")
        publication["status"] = "tls_pending"
        helper.save_state(publication_path, publication)
        changed = True
    if helper.GUARD.exists():
        attestation = helper.private_json(helper.GUARD)
        if (
            attestation.get("instance_uuid") != config["instance_uuid"]
            or attestation.get("domain") != config["domain"]
            or attestation.get("guard_kind") != "host_raw"
        ):
            raise SafeError("foreign_publication_attestation")
        helper.GUARD.unlink()
    if publication["status"] == "tls_pending":
        restart(helper)
        publication["status"] = "verifying"
        helper.save_state(publication_path, publication)
        changed = True
    verify()
    publication["status"] = "verified"
    helper.save_state(publication_path, publication)
    return {"changed": changed, "comment": "client_production_ready"}


def boot_guard(helper, guard):
    if not guard.RECEIPT.exists():
        return
    receipt = helper.private_json(guard.RECEIPT)
    if receipt.get("status") in ("closing", "closed", "partial"):
        binding = guard.identity(receipt["instance_uuid"], receipt["public_ip"])
        guard.apply("close", binding)


def main():
    if os.geteuid() != 0:
        raise SafeError("root_required")
    helper = load("oduflow-create-production")
    guard = load("oduflow-publication-guard")
    helper.STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in (helper.STATE_DIR, guard.DIRECTORY):
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise SafeError("unsafe_state_directory")
    # Same locks as standalone creation/guard commands prevent concurrent operators.
    with ExitStack() as stack:
        for path in (helper.STATE_DIR / "lock", guard.DIRECTORY / "host-publication-guard.lock"):
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            stream = stack.enter_context(os.fdopen(fd, "w"))
            fcntl.flock(stream, fcntl.LOCK_EX)
        if sys.argv[1:] == ["--boot-guard"]:
            boot_guard(helper, guard)
            return {"changed": False, "comment": "publication_boot_guard_checked"}
        if sys.argv[1:]:
            raise SafeError("unsupported_arguments")
        config = helper.validate(helper.private_json("/etc/oduflow/production.json"))
        helper.verify_runtime_config(config)
        network = helper.private_json(PUBLICATION_CONFIG)
        if network.get("instance_uuid") != config["instance_uuid"]:
            raise SafeError("network_identity_mismatch")
        binding = guard.identity(config["instance_uuid"], network.get("public_ipv4"))
        return execute(helper, guard, config, binding)


if __name__ == "__main__":
    try:
        print(json.dumps(main()))
    except Exception:
        print(
            json.dumps(
                {
                    "changed": False,
                    "comment": "client_production_not_ready; inspect receipt and guard status",
                }
            )
        )
        raise SystemExit(1) from None
