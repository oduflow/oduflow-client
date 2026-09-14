#!/usr/bin/env python3
"""Stage a checksum-pinned private source archive without distributing credentials."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True, help="Local Paseo Git checkout")
    parser.add_argument(
        "--output", type=Path, required=True, help="Master build context client-artifacts directory"
    )
    args = parser.parse_args()
    manifest = Path(__file__).resolve().parents[1] / "salt/states/client_apps/artifacts.json"
    spec = json.loads(manifest.read_text())["paseo"]
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = args.output / ("paseo-" + spec["commit"] + ".tar.gz")
    if target.exists():
        raise SystemExit("Refusing to replace an existing source artifact")
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(args.repository),
                "archive",
                "--format=tar.gz",
                "--output=" + str(target.resolve()),
                spec["commit"],
            ],
            check=True,
        )
        target.chmod(0o600)
        with target.open("rb") as stream:
            actual = "sha256=" + hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != spec["archive"]["hash"]:
            raise ValueError("Generated source archive does not match the pinned checksum")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    print(f"Verified source archive: {target.name}")


if __name__ == "__main__":
    main()
