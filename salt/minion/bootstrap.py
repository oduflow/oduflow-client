#!/usr/bin/python3
"""Enroll a host with installed Salt 3006, VPN joined and pre-issued keys."""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import uuid
from pathlib import Path


def fingerprint(pem):
    # Salt pem_finger hashes the PEM body, including embedded newlines, not DER.
    body = "".join(line for line in pem.splitlines(keepends=True) if line.strip())
    body = "".join(body.splitlines(keepends=True)[1:-1]).replace("\r\n", "\n").encode()
    digest = hashlib.sha256(body).hexdigest()
    return ":".join(digest[index : index + 2] for index in range(0, len(digest), 2))


def configuration(instance_uuid, master, master_fingerprint):
    if str(uuid.UUID(instance_uuid)) != instance_uuid:
        raise ValueError("instance UUID must be canonical")
    if ipaddress.ip_address(master) not in ipaddress.ip_network("100.64.0.0/10"):
        raise ValueError("master must be its explicit Headscale IPv4 address")
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){31}[0-9a-f]{2}", master_fingerprint):
        raise ValueError("master fingerprint must be colon-separated SHA256")
    return {
        "id": "client-" + instance_uuid,
        "master": master,
        "master_finger": master_fingerprint,
        "hash_type": "sha256",
        "open_mode": False,
        "file_client": "remote",
        "cache_jobs": False,
        "minion_pillar_cache": False,
        "log_level": "warning",
        "log_level_logfile": "warning",
        "grains": {"role": "client-stack", "oduflow": {"instance_uuid": instance_uuid}},
    }


def write_private(path, contents):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(contents)
    os.replace(temporary, path)


def transport_configuration():
    # Salt 3006 applies these to its ZeroMQ SUB and request sockets. The
    # scheduled master check observes TCP state; keepalives first expose a
    # half-open connection after a userspace VPN gateway is recreated.
    return {
        "tcp_keepalive": True,
        "tcp_keepalive_idle": 30,
        "tcp_keepalive_intvl": 10,
        "tcp_keepalive_cnt": 3,
        "master_alive_interval": 30,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-uuid", required=True)
    parser.add_argument("--master", required=True)
    parser.add_argument("--master-fingerprint", required=True)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--public-key", required=True, type=Path)
    args = parser.parse_args()
    config = configuration(args.instance_uuid, args.master, args.master_fingerprint)
    version = subprocess.check_output(["salt-minion", "--version"], text=True)
    if not re.search(r"\b3006\.\d+\b", version):
        raise ValueError("host must have a pinned Salt 3006 onedir package installed")
    public = args.public_key.read_text()
    private = args.private_key.read_text()
    derived = subprocess.check_output(
        ["openssl", "pkey", "-pubout", "-in", str(args.private_key)], text=True
    )
    if derived.strip() != public.strip():
        raise ValueError("minion public/private key pair does not match")
    config_path = Path("/etc/salt/minion.d/oduflow.conf")
    if config_path.exists() and json.loads(config_path.read_text()) != config:
        raise ValueError("existing enrollment differs; refuse identity or master replacement")
    pki = Path("/etc/salt/pki/minion")
    for name, desired in [("minion.pem", private), ("minion.pub", public)]:
        existing = pki / name
        if existing.exists() and existing.read_text().strip() != desired.strip():
            raise ValueError("existing minion keys differ; refuse identity replacement")
    subprocess.run(["systemctl", "stop", "salt-minion"], check=True)
    write_private(pki / "minion.pem", private)
    write_private(pki / "minion.pub", public)
    write_private(config_path, json.dumps(config, indent=2) + "\n")
    write_private(
        "/etc/salt/minion.d/oduflow-transport.conf",
        json.dumps(transport_configuration(), indent=2) + "\n",
    )
    write_private(
        "/etc/oduflow/instance.json",
        json.dumps({"instance_uuid": args.instance_uuid, "minion_id": config["id"]}) + "\n",
    )
    # The clean-host installer masks the daemon until identity and trust are installed.
    subprocess.run(["systemctl", "unmask", "salt-minion"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "salt-minion"], check=True)
    print(json.dumps({"minion_id": config["id"], "public_key_sha256": fingerprint(public)}))


if __name__ == "__main__":
    main()
