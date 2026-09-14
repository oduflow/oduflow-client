#!/usr/bin/python3
"""Create a cold restic backup and prove a scoped restore without exposing secrets."""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

SOURCE = Path("/srv/oduflow/data")
STORAGE_RECEIPT = Path("/etc/oduflow/storage.json")
PROBE = SOURCE / ".oduflow-backup" / "restore-probe.json"
UNITS = (
    "oduflow.service",
    "paseo-proxy.socket",
    "paseo.service",
    # Stop socket activation before the daemon so probes cannot reopen the
    # client's databases while the data volume is being copied.
    "docker.socket",
    "docker.service",
    "containerd.service",
)
RESTART_ORDER = tuple(reversed(UNITS))


class BackupError(RuntimeError):
    pass


def run(*args, capture=False, check=True, error="backup_command_failed"):
    completed = subprocess.run(
        args,
        check=False,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        env=os.environ.copy(),
    )
    if check and completed.returncode:
        raise BackupError(error)
    return completed


def validate_environment():
    required = (
        "RESTIC_REPOSITORY",
        "RESTIC_PASSWORD",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "ODUFLOW_INSTANCE_UUID",
        "ODUFLOW_BACKUP_RETENTION_DAYS",
    )
    if any(not os.environ.get(name) for name in required):
        raise BackupError("Backup environment is incomplete")
    instance_uuid = os.environ["ODUFLOW_INSTANCE_UUID"]
    if str(uuid.UUID(instance_uuid)) != instance_uuid:
        raise BackupError("Backup instance identity is invalid")
    try:
        retention = int(os.environ["ODUFLOW_BACKUP_RETENTION_DAYS"])
    except ValueError:
        raise BackupError("Backup retention is invalid") from None
    if not 1 <= retention <= 3650:
        raise BackupError("Backup retention is invalid")
    if not os.path.ismount(SOURCE):
        raise BackupError("Client data volume is not mounted")
    try:
        receipt = json.loads(STORAGE_RECEIPT.read_text())
    except (OSError, ValueError):
        raise BackupError("Storage ownership receipt is unavailable") from None
    if receipt.get("instance_uuid") != instance_uuid:
        raise BackupError("Storage ownership does not match backup identity")
    return instance_uuid, retention


def ensure_probe(instance_uuid):
    expected = (
        json.dumps(
            {"instance_uuid": instance_uuid, "purpose": "restore-verification"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    PROBE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if PROBE.exists() and PROBE.read_bytes() != expected:
        raise BackupError("Restore probe identity conflicts with this client")
    if not PROBE.exists():
        temporary = PROBE.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(expected)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, PROBE)
    return expected


def active_units():
    return [
        unit
        for unit in UNITS
        if run("systemctl", "is-active", "--quiet", unit, check=False).returncode == 0
    ]


def stop_units(units):
    for unit in units:
        run("systemctl", "stop", unit, error="client_service_stop_failed")


def restart_units(units):
    failure = None
    for unit in RESTART_ORDER:
        if unit in units:
            try:
                run("systemctl", "start", unit, error="client_service_restart_failed")
            except Exception as exc:
                failure = failure or exc
    if failure:
        raise failure


def ensure_repository():
    if run("restic", "cat", "config", check=False).returncode == 0:
        return False
    # Restic init is reconcilable: a lost response is followed only by `cat config`.
    run("restic", "init", check=False)
    if run("restic", "cat", "config", check=False).returncode != 0:
        raise BackupError("Restic repository is unavailable")
    return True


def latest_snapshot(instance_uuid):
    completed = run(
        "restic",
        "snapshots",
        "--json",
        "--latest",
        "1",
        "--host",
        "client-" + instance_uuid,
        capture=True,
        error="restic_snapshot_list_failed",
    )
    try:
        snapshots = json.loads(completed.stdout)
    except ValueError:
        raise BackupError("Restic snapshot response is invalid") from None
    if not isinstance(snapshots, list) or len(snapshots) != 1:
        raise BackupError("Expected exactly one latest client snapshot")
    snapshot_id = snapshots[0].get("id")
    if not isinstance(snapshot_id, str) or len(snapshot_id) != 64:
        raise BackupError("Restic snapshot identity is invalid")
    return snapshot_id


def verify_restore(instance_uuid, expected):
    snapshot_id = latest_snapshot(instance_uuid)
    with tempfile.TemporaryDirectory(prefix="oduflow-restore-") as target:
        run(
            "restic",
            "restore",
            snapshot_id,
            "--target",
            target,
            "--include",
            str(PROBE),
            error="restic_restore_failed",
        )
        restored = Path(target) / str(PROBE).removeprefix("/")
        if not restored.is_file() or restored.read_bytes() != expected:
            raise BackupError("Restored verification object does not match the backup")
    run("restic", "check", error="restic_repository_check_failed")
    return hashlib.sha256(expected).hexdigest()


def backup(instance_uuid, retention, verify):
    expected = ensure_probe(instance_uuid)
    repository_created = ensure_repository()
    running = active_units()
    failure = None
    try:
        stop_units(running)
        run(
            "restic",
            "backup",
            str(SOURCE),
            "--host",
            "client-" + instance_uuid,
            "--tag",
            "oduflow-client-data",
            "--exclude-caches",
            error="restic_backup_failed",
        )
    except Exception as exc:
        failure = exc
    finally:
        try:
            restart_units(running)
        except Exception as exc:
            failure = failure or exc
    if failure:
        raise failure
    digest = verify_restore(instance_uuid, expected) if verify else None
    run(
        "restic",
        "forget",
        "--host",
        "client-" + instance_uuid,
        "--keep-within",
        f"{retention}d",
        "--prune",
        error="restic_retention_failed",
    )
    return repository_created, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-restore", action="store_true")
    args = parser.parse_args()
    try:
        instance_uuid, retention = validate_environment()
        with open("/run/oduflow-backup.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            repository_created, digest = backup(instance_uuid, retention, args.verify_restore)
        print(
            json.dumps(
                {
                    "changed": True,
                    "comment": (
                        "Client data backup and scoped restore verified"
                        if digest
                        else "Client data backup completed"
                    ),
                    "repository_initialized": repository_created,
                    "restore_verified": bool(digest),
                }
            )
        )
    except BackupError as exc:
        print(json.dumps({"changed": False, "comment": str(exc)}))
        sys.exit(1)
    except (OSError, ValueError, subprocess.CalledProcessError, shutil.Error):
        print(json.dumps({"changed": False, "comment": "client_data_backup_failed"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
