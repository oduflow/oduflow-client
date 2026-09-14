#!/usr/bin/python3
"""Final offline image-builder step. Never run on an enrolled/client machine."""

import argparse
import shutil
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-disposable-image", action="store_true", required=True)
    parser.parse_args()
    if Path("/etc/oduflow/instance.json").exists() or Path("/etc/oduflow/storage.json").exists():
        raise SystemExit("Refusing cleanup of an enrolled instance")
    for service in ("salt-minion", "tailscaled"):
        subprocess.run(["systemctl", "stop", service], check=True)
    subprocess.run(["systemctl", "disable", "salt-minion"], check=True)
    for name in ("/etc/salt/pki/minion", "/var/cache/salt/minion", "/var/lib/tailscale"):
        path = Path(name)
        if path.exists():
            shutil.rmtree(path)
    for name in (
        "/etc/salt/minion_id",
        "/etc/salt/grains",
        "/etc/salt/minion.d/oduflow.conf",
        "/var/lib/dbus/machine-id",
        "/var/lib/systemd/random-seed",
    ):
        Path(name).unlink(missing_ok=True)
    for key in Path("/etc/ssh").glob("ssh_host_*"):
        key.unlink()
    # cloud-init cleans instance data and arranges regeneration of machine-id.
    subprocess.run(["cloud-init", "clean", "--logs", "--machine-id", "--seed"], check=True)
    print("Identity cleanup complete. Shut down now; do not boot before snapshot.")


if __name__ == "__main__":
    main()
