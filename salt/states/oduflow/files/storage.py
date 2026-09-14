#!/usr/bin/python3
"""Fail-closed whole-device XFS preparation. Run only on the client as root."""

import argparse
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path


class UnsafeStorage(RuntimeError):
    pass


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def canonical_uuid(value):
    if str(uuid.UUID(value)) != value:
        raise UnsafeStorage("Instance UUID must be canonical")
    return value


def validate_device_name(device):
    if not re.fullmatch(r"/dev/disk/by-id/[A-Za-z0-9_.:+-]+", device):
        raise UnsafeStorage("Storage device must be a stable /dev/disk/by-id path")


def wait_device(device, timeout):
    deadline = time.monotonic() + timeout
    while not os.path.exists(device):
        if time.monotonic() >= deadline:
            raise UnsafeStorage("Timed out waiting for attached storage")
        time.sleep(1)
    if not Path(device).is_symlink() or not stat.S_ISBLK(os.stat(device).st_mode):
        raise UnsafeStorage("Expected by-id symlink to a block device")
    return str(Path(device).resolve())


def inspect(device):
    data = json.loads(
        run("lsblk", "--json", "--paths", "--output", "NAME,TYPE,MAJ:MIN,RO,RM", device)
    )["blockdevices"]
    if len(data) != 1:
        raise UnsafeStorage("Expected exactly one storage device")
    disk = data[0]
    if disk["type"] != "disk" or disk.get("children") or disk["ro"] or disk["rm"]:
        raise UnsafeStorage("Refusing partitioned, layered, read-only or removable disk")
    return disk


def mounts():
    tree = json.loads(run("findmnt", "--json", "--output", "SOURCE,TARGET,FSTYPE,OPTIONS,MAJ:MIN"))[
        "filesystems"
    ]
    result = []

    def visit(nodes):
        for node in nodes:
            result.append(node)
            visit(node.get("children", []))

    visit(tree)
    return result


def check_mounts(disk, entries, mountpoint, owned_filesystem=False):
    owned = []
    # A running Docker/Odoo host has overlay mounts beneath its data mount.
    # They are safe for verification only after matching recorded filesystem
    # ownership and confirming that the parent itself is the exact XFS device.
    allow_nested = owned_filesystem and any(
        entry["target"] == mountpoint
        and entry["maj:min"] == disk["maj:min"]
        and entry["fstype"] == "xfs"
        for entry in entries
    )
    for entry in entries:
        target = entry["target"]
        same_device = entry["maj:min"] == disk["maj:min"]
        if same_device and target != mountpoint:
            raise UnsafeStorage("Disk is mounted elsewhere (possibly system or swap)")
        if target == mountpoint:
            if not same_device or entry["fstype"] != "xfs":
                raise UnsafeStorage("Mountpoint occupied by another device or filesystem")
            owned.append(entry)
        elif target.startswith(mountpoint + "/") and not allow_nested:
            raise UnsafeStorage("Mountpoint contains nested mounts")
    return owned


def get_filesystem(device):
    # wipefs reports signatures without modifying them; a probe error is fatal.
    signatures = json.loads(run("wipefs", "--json", "--output", "TYPE", device))["signatures"]
    kinds = {item["type"] for item in signatures}
    if not kinds:
        return None
    if kinds != {"xfs"}:
        raise UnsafeStorage("Refusing non-XFS filesystem, partition table, RAID or swap signature")
    return run("blkid", "-p", "-s", "UUID", "-o", "value", device).strip()


def prepare(args):
    canonical_uuid(args.instance_uuid)
    validate_device_name(args.device)
    if args.mount != "/srv/oduflow/data":
        raise UnsafeStorage("Unsupported mountpoint")
    device = wait_device(args.device, args.timeout)
    disk = inspect(device)
    # /proc/swaps covers active swap even when it has no filesystem mount.
    for line in Path("/proc/swaps").read_text().splitlines()[1:]:
        if str(Path(line.split()[0]).resolve()) == device:
            raise UnsafeStorage("Refusing active swap device")
    target = Path(args.mount)
    if target.is_symlink():
        raise UnsafeStorage("Mountpoint must not be a symlink")
    receipt_path = Path("/etc/oduflow/storage.json")
    receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
    if receipt and (
        receipt.get("instance_uuid") != args.instance_uuid or receipt.get("device") != args.device
    ):
        raise UnsafeStorage("Storage identity conflicts with recorded instance ownership")
    expected_uuid = args.filesystem_uuid or receipt.get("filesystem_uuid")
    if args.filesystem_uuid and receipt and receipt.get("filesystem_uuid") != args.filesystem_uuid:
        raise UnsafeStorage("Filesystem UUID conflicts with local ownership record")
    fs_uuid = get_filesystem(device)
    mounted = check_mounts(
        disk,
        mounts(),
        args.mount,
        owned_filesystem=bool(expected_uuid and fs_uuid == expected_uuid),
    )
    if not mounted and target.exists() and any(target.iterdir()):
        raise UnsafeStorage("Refusing to hide existing files under a new mount")
    changed = False
    if not fs_uuid:
        if args.verify or not args.allow_format or expected_uuid or mounted:
            raise UnsafeStorage(
                "Blank volume requires explicit allow_format and no existing ownership"
            )
        # Re-probe topology immediately before formatting; never pass mkfs -f.
        if str(Path(args.device).resolve()) != device or inspect(device) != disk:
            raise UnsafeStorage("Device identity changed during preparation")
        if check_mounts(disk, mounts(), args.mount):
            raise UnsafeStorage("Disk became mounted during preparation")
        if get_filesystem(device):
            raise UnsafeStorage("Filesystem appeared during preparation")
        run("mkfs.xfs", device)
        fs_uuid = get_filesystem(device)
        if not fs_uuid:
            raise UnsafeStorage("Formatting did not produce a filesystem UUID")
        expected_uuid = fs_uuid
        changed = True
    if not expected_uuid or fs_uuid != expected_uuid:
        raise UnsafeStorage("Existing XFS UUID must match explicit or recorded ownership")
    if args.verify:
        if len(mounted) != 1 or not (
            {"prjquota", "pquota"} & set(mounted[0]["options"].split(","))
        ):
            raise UnsafeStorage("Required XFS project-quota mount is not active")
        if "rw" not in mounted[0]["options"].split(","):
            raise UnsafeStorage("Storage is not writable")
    elif not receipt:
        receipt_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {
            "instance_uuid": args.instance_uuid,
            "device": args.device,
            "filesystem_uuid": fs_uuid,
        }
        temporary = receipt_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, receipt_path)
        changed = True
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--instance-uuid", required=True)
    parser.add_argument("--mount", default="/srv/oduflow/data")
    parser.add_argument("--filesystem-uuid")
    parser.add_argument("--allow-format", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if not 0 <= args.timeout <= 600:
        parser.error("timeout must be between 0 and 600 seconds")
    try:
        with open("/run/oduflow-storage.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            changed = prepare(args)
        print(
            json.dumps({"changed": changed, "comment": "Storage identity and safety checks passed"})
        )
    except (UnsafeStorage, OSError, ValueError, subprocess.CalledProcessError) as exc:
        # Do not echo subprocess output: other commands may contain credentials.
        print(json.dumps({"changed": False, "comment": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
