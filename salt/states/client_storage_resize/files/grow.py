#!/usr/bin/env python3
"""Grow an owned, mounted whole-device XFS online; never format or stop services."""

import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

MOUNT = "/srv/oduflow/data"
CONFIG = Path("/etc/oduflow/storage-resize.json")
OWNERSHIP = Path("/etc/oduflow/storage.json")


class UnsafeResize(Exception):
    pass


def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 65536
        ):
            raise UnsafeResize("invalid_private_configuration")
        result = json.load(stream)
    if not isinstance(result, dict):
        raise UnsafeResize("invalid_private_configuration")
    return result


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=120).stdout


def geometry():
    text = run("xfs_info", MOUNT)
    match = re.search(r"^data\s*=\s*bsize=(\d+)\s+blocks=(\d+)", text, re.MULTILINE)
    if not match:
        raise UnsafeResize("xfs_geometry_unavailable")
    return int(match[1]), int(match[2])


def load_storage():
    path = Path("/usr/local/libexec/oduflow-storage")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise UnsafeResize("storage_helper_untrusted")
    module = types.ModuleType("storage")
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


def grow(config, storage, *, clock=time.monotonic, sleep=time.sleep):
    for key in ("instance_uuid", "request_id"):
        if str(UUID(config[key])) != config[key]:
            raise UnsafeResize("invalid_resize_identity")
    target = config.get("target_size_gb")
    if type(target) is not int or not 1 <= target <= 10000:
        raise UnsafeResize("invalid_resize_target")
    owner = private_json(OWNERSHIP)
    if (
        owner.get("instance_uuid") != config["instance_uuid"]
        or owner.get("device") != config["device"]
        or not owner.get("filesystem_uuid")
    ):
        raise UnsafeResize("storage_identity_mismatch")
    args = SimpleNamespace(
        instance_uuid=config["instance_uuid"],
        device=config["device"],
        filesystem_uuid=owner["filesystem_uuid"],
        mount=MOUNT,
        verify=True,
        allow_format=False,
        timeout=0,
    )
    storage.prepare(args)  # Exact mount/device/UUID, rw XFS, no partitions, project quotas.
    device = Path(config["device"]).resolve()
    if not re.fullmatch(r"(?:vd|sd)[a-z]+|nvme\d+n\d+", device.name):
        raise UnsafeResize("unsupported_whole_device")
    before = geometry()
    target_bytes = target * 1024**3
    if before[0] * before[1] > target_bytes:
        raise UnsafeResize("filesystem_larger_than_requested")
    rescan = Path("/sys/class/block") / device.name / "device/rescan"
    if rescan.exists():
        rescan.write_text("1\n")
    # Virtio-blk receives a capacity change event automatically; it has no
    # SCSI rescan file. Wait for that event without detach/reboot/BLKRRPART.
    deadline = clock() + 120
    while True:
        actual_bytes = int(run("blockdev", "--getsize64", str(device)).strip())
        if actual_bytes == target_bytes:
            break
        if actual_bytes > target_bytes:
            raise UnsafeResize("device_larger_than_requested")
        if clock() >= deadline:
            raise UnsafeResize("kernel_capacity_not_ready")
        sleep(min(2, max(0, deadline - clock())))
    storage.prepare(args)  # Reverify ownership immediately before grow.
    if Path(config["device"]).resolve() != device:
        raise UnsafeResize("device_identity_changed")
    target_blocks = target_bytes // before[0]
    changed = before[1] < target_blocks
    if changed:
        run("xfs_growfs", "-D", str(target_blocks), MOUNT)
    storage.prepare(args)
    after = geometry()
    if after[0] != before[0] or after[1] != target_blocks:
        raise UnsafeResize("filesystem_growth_unconfirmed")
    return {"changed": changed, "comment": "client_storage_resize_ready"}


def main():
    if os.geteuid() != 0:
        raise UnsafeResize("root_required")
    with open("/run/oduflow-storage.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return grow(private_json(CONFIG), load_storage())


if __name__ == "__main__":
    try:
        print(json.dumps(main()))
    except Exception:
        print(json.dumps({"changed": False, "comment": "client_storage_resize_failed"}))
        sys.exit(1)
