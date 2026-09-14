#!/usr/bin/python3
"""Suspend owned client workloads and resume the same services and containers."""

import argparse
import fcntl
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path("/var/lib/oduflow/lifecycle")
MARKER = ROOT / "suspended"
RECEIPT = ROOT / "receipt.json"
STORAGE = Path("/etc/oduflow/storage.json")
SYSTEMD = Path("/etc/systemd/system")
UNITS = (
    "oduflow.service",
    "paseo-proxy.socket",
    "paseo-proxy.service",
    "paseo.service",
    "docker.socket",
    "docker.service",
    "containerd.service",
)
GUARD = "[Unit]\nConditionPathExists=!/var/lib/oduflow/lifecycle/suspended\n"


class LifecycleError(RuntimeError):
    pass


def run(*args, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=300)
    if check and result.returncode:
        raise LifecycleError("client_lifecycle_command_failed")
    return result


def store(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(data, sort_keys=True))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def verify_storage(instance_uuid):
    owner = json.loads(STORAGE.read_text())
    if owner.get("instance_uuid") != instance_uuid or not owner.get("filesystem_uuid"):
        raise LifecycleError("client_lifecycle_storage_identity_mismatch")
    # The installed executable has no Python suffix.
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("lifecycle_storage", "/usr/local/libexec/oduflow-storage")
    spec = importlib.util.spec_from_loader(loader.name, loader)
    storage = importlib.util.module_from_spec(spec)
    loader.exec_module(storage)
    storage.prepare(
        SimpleNamespace(
            instance_uuid=instance_uuid,
            device=owner["device"],
            filesystem_uuid=owner["filesystem_uuid"],
            mount="/srv/oduflow/data",
            verify=True,
            allow_format=False,
            timeout=0,
        )
    )


def active_units():
    return [
        unit
        for unit in UNITS
        if run("systemctl", "is-active", "--quiet", unit, check=False).returncode == 0
    ]


def containers():
    ids = run("docker", "ps", "--no-trunc", "--quiet").stdout.split()
    if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in ids):
        raise LifecycleError("client_lifecycle_container_identity_invalid")
    return ids


def apply(instance_uuid, cycle, target):
    if str(uuid.UUID(instance_uuid)) != instance_uuid or cycle < 1:
        raise LifecycleError("client_lifecycle_identity_invalid")
    verify_storage(instance_uuid)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    if ROOT.is_symlink() or ROOT.stat().st_uid != os.geteuid():
        raise LifecycleError("client_lifecycle_receipt_path_invalid")
    previous = json.loads(RECEIPT.read_text()) if RECEIPT.exists() else None
    if previous and (
        previous["instance_uuid"] != instance_uuid
        or previous["cycle"] > cycle
        or any(unit not in UNITS for unit in previous["units"])
        or any(not re.fullmatch(r"[0-9a-f]{64}", cid) for cid in previous["containers"])
    ):
        raise LifecycleError("client_lifecycle_receipt_conflict")
    if target == "suspend":
        if previous and previous["cycle"] == cycle and previous["phase"] in ("resuming", "active"):
            raise LifecycleError("client_lifecycle_stale_suspend")
        if not previous or previous["cycle"] != cycle:
            if previous and previous["phase"] != "active":
                raise LifecycleError("client_lifecycle_previous_unresolved")
            units = active_units()
            previous = dict(
                instance_uuid=instance_uuid,
                cycle=cycle,
                phase="suspending",
                units=units,
                containers=containers() if "docker.service" in units else [],
            )
            store(RECEIPT, previous)
        # Conditions survive reboot and do not overwrite or unmask existing units.
        for unit in UNITS:
            directory = SYSTEMD / (unit + ".d")
            directory.mkdir(parents=True, exist_ok=True)
            guard = directory / "90-oduflow-lifecycle.conf"
            if guard.exists() and guard.read_text() != GUARD:
                raise LifecycleError("client_lifecycle_guard_conflict")
            guard.write_text(GUARD)
        store(MARKER, {"instance_uuid": instance_uuid, "cycle": cycle})
        run("systemctl", "daemon-reload")
        # Stop admission first, then gracefully stop databases before Docker.
        for unit in UNITS[:4]:
            run("systemctl", "stop", unit)
        if "docker.service" in active_units():
            running = containers()
            # Include workloads admitted between the initial inventory and stop.
            previous["containers"] = sorted(set(previous["containers"]) | set(running))
            store(RECEIPT, previous)
            if running:
                run("docker", "stop", "--time", "60", *running)
        for unit in UNITS[4:]:
            run("systemctl", "stop", unit)
        if active_units():
            raise LifecycleError("client_lifecycle_stop_not_verified")
        previous["phase"] = "suspended"
        store(RECEIPT, previous)
    else:
        if not previous or previous["cycle"] != cycle or previous["phase"] == "suspending":
            raise LifecycleError("client_lifecycle_suspend_not_verified")
        previous["phase"] = "resuming"
        store(RECEIPT, previous)
        MARKER.unlink(missing_ok=True)
        run("systemctl", "daemon-reload")
        for unit in reversed(UNITS):
            if unit in previous["units"]:
                run("systemctl", "start", unit)
        if previous["containers"]:
            run("docker", "start", *previous["containers"])
        if not set(previous["units"]).issubset(active_units()):
            raise LifecycleError("client_lifecycle_start_not_verified")
        if previous["containers"] and not set(previous["containers"]).issubset(containers()):
            raise LifecycleError("client_lifecycle_containers_not_verified")
        run("/usr/local/libexec/oduflow-client-health")
        run("/usr/local/libexec/oduflow-verify-production")
        previous["phase"] = "active"
        store(RECEIPT, previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance_uuid")
    parser.add_argument("cycle", type=int)
    parser.add_argument("target", choices=("suspend", "resume"))
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise LifecycleError("client_lifecycle_requires_root")
        # Backup and lifecycle changes must never interleave stop/start inventories.
        with open("/run/oduflow-backup.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            apply(args.instance_uuid, args.cycle, args.target)
        print(json.dumps({"changed": True, "comment": "Client lifecycle verified"}))
    except Exception:
        # Command output, runtime settings and Salt returns stay private.
        print(json.dumps({"changed": False, "comment": "client_lifecycle_failed"}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
