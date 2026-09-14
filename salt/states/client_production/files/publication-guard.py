#!/usr/bin/python3
"""Temporary exact-interface HTTP ingress block before Docker DNAT; no firewall flush."""

import argparse
import fcntl
import ipaddress
import json
import os
import re
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

DIRECTORY = Path("/etc/oduflow")
RECEIPT = DIRECTORY / "host-publication-guard.json"


def run(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=15)


def identity(instance_uuid, public_ip):
    if (
        str(uuid.UUID(instance_uuid)) != instance_uuid
        or not ipaddress.IPv4Address(public_ip).is_global
    ):
        raise ValueError("Invalid canonical identity or public IPv4")
    local = json.loads((DIRECTORY / "instance.json").read_text())
    if local.get("instance_uuid") != instance_uuid:
        raise ValueError("Client identity mismatch")
    # Find the assigned interface; a route to our own address would report lo.
    result = run(["/usr/sbin/ip", "-json", "address", "show"])
    if result.returncode:
        raise ValueError("Cannot inspect interface addresses")
    matches = [
        entry["ifname"]
        for entry in json.loads(result.stdout)
        if any(
            addr.get("local") == public_ip and addr.get("scope") == "global"
            for addr in entry.get("addr_info", [])
        )
    ]
    if (
        len(matches) != 1
        or not re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", matches[0])
        or matches[0].startswith(("lo", "tailscale", "docker", "br-", "veth"))
    ):
        raise ValueError("Expected exactly one public physical interface")
    return {"instance_uuid": instance_uuid, "public_ip": public_ip, "interface": matches[0]}


def rule(binary, action, binding):
    args = [binary, "-w", "5", "-t", "raw", action, "PREROUTING"]
    if action == "-I":
        args.append("1")
    args += [
        "-i",
        binding["interface"],
        "-p",
        "tcp",
        "-m",
        "multiport",
        "--dports",
        "80,443",
        "-m",
        "comment",
        "--comment",
        "oduflow-publication-" + binding["instance_uuid"],
        "-j",
        "DROP",
    ]
    result = run(args)
    if result.returncode and not (action == "-C" and result.returncode == 1):
        raise ValueError("Publication firewall command failed")
    return result.returncode == 0


def store(record):
    with tempfile.NamedTemporaryFile(dir=DIRECTORY, delete=False) as stream:
        temporary = Path(stream.name)
        os.fchmod(stream.fileno(), 0o600)
        stream.write((json.dumps(record) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, RECEIPT)
    fd = os.open(DIRECTORY, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def apply(action, binding):
    if RECEIPT.exists() or RECEIPT.is_symlink():
        info = RECEIPT.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("Invalid guard receipt permissions")
        old = json.loads(RECEIPT.read_text())
        if any(old.get(key) != value for key, value in binding.items()):
            raise ValueError("Existing guard belongs to another interface or client")
    elif action == "restore":
        raise ValueError("Cannot restore without an owned guard receipt")
    binaries = ["/usr/sbin/iptables", "/usr/sbin/ip6tables"]
    if action in {"close", "restore"}:
        store({**binding, "status": "closing" if action == "close" else "restoring"})
        for binary in binaries:
            present = rule(binary, "-C", binding)
            if action == "close" and not present:
                rule(binary, "-I", binding)
            elif action == "restore":
                while present:
                    rule(binary, "-D", binding)
                    present = rule(binary, "-C", binding)
    present = [rule(binary, "-C", binding) for binary in binaries]
    status = "closed" if all(present) else "open" if not any(present) else "partial"
    record = {
        **binding,
        "status": status,
        "ipv4": present[0],
        "ipv6": present[1],
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    }
    if action != "status":
        store(record)
    if action == "close" and status != "closed" or action == "restore" and status != "open":
        raise ValueError("Publication guard verification failed")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["close", "status", "restore"])
    parser.add_argument("--instance-uuid", required=True)
    parser.add_argument("--public-ip", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError("Run as root")
    binding = identity(args.instance_uuid, args.public_ip)
    info = DIRECTORY.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("Guard directory must be root-owned and not writable by others")
    fd = os.open(
        DIRECTORY / "host-publication-guard.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        print(json.dumps(apply(args.action, binding)))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Publication guard failed; inspect rules before proceeding") from None
