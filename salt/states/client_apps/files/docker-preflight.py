#!/usr/bin/python3
"""Refuse implicit migration of an existing Docker/containerd installation."""

import json
import sys
from pathlib import Path


def check():
    # Changing daemon storage on a populated host could orphan containers. A
    # completed earlier deployment leaves these old paths absent or empty.
    for directory in ("/var/lib/docker", "/var/lib/containerd"):
        path = Path(directory)
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise ValueError("Existing container data requires an explicit migration")
    path = Path("/etc/docker/daemon.json")
    if path.exists():
        config = json.loads(path.read_text())
        if config.get("data-root") != "/srv/oduflow/data/docker":
            raise ValueError("Existing Docker configuration requires an explicit migration")
        if config.get("bip", "172.17.0.1/16") != "172.17.0.1/16":
            raise ValueError("Existing Docker bridge conflicts with the client network")


if __name__ == "__main__":
    try:
        check()
    except (OSError, ValueError):
        sys.exit("Docker preflight refused existing data or incompatible configuration")
    print(
        json.dumps({"changed": False, "comment": "Clean or previously managed container storage"})
    )
