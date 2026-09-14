#!/usr/bin/env python3
"""Build a credential-free Paseo runtime archive on the target Ubuntu release."""

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import tarfile
from pathlib import Path
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ubuntu", default="26.04")
    args = parser.parse_args()
    host = platform.freedesktop_os_release()
    if (
        host.get("ID") != "ubuntu"
        or host.get("VERSION_ID") != args.ubuntu
        or platform.machine() != "x86_64"
    ):
        raise SystemExit("Build on the requested Ubuntu amd64 release")
    root = Path(__file__).resolve().parents[1]
    states = root / "salt/states/client_apps"
    artifacts = json.loads((states / "artifacts.json").read_text())
    spec = importlib.util.spec_from_file_location("installer", states / "files/install.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    installer.CACHE.mkdir(parents=True, exist_ok=True)
    installer.MANIFEST.write_text(json.dumps(artifacts))
    node_spec = artifacts["node"]
    node_archive = installer.CACHE / "node.tar.xz"
    with urlopen(node_spec["url"], timeout=60) as response, node_archive.open("wb") as output:
        shutil.copyfileobj(response, output)
    with node_archive.open("rb") as stream:
        if "sha256=" + hashlib.file_digest(stream, "sha256").hexdigest() != node_spec["hash"]:
            raise SystemExit("Node checksum mismatch")
    node_root = installer.ROOT / "node" / node_spec["version"]
    node_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(node_archive) as archive:
        archive.extractall(node_root, filter="data")
    paseo = artifacts["paseo"]
    source_name = "paseo-" + paseo["commit"] + ".tar.gz"
    shutil.copyfile(states / "artifacts" / source_name, installer.CACHE / source_name)
    installer.install("paseo")
    target = installer.ROOT / "paseo" / installer.release("paseo", paseo)
    args.output.mkdir(parents=True, exist_ok=True)
    metadata = {
        "contract": 1,
        "os": "ubuntu",
        "os_version": args.ubuntu,
        "architecture": "x86_64",
        "node_version": node_spec["version"],
        "version": paseo["version"],
        "commit": paseo["commit"],
    }
    metadata_path = args.output / "runtime.json"
    metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n")
    filename = f"paseo-{paseo['commit']}-ubuntu{args.ubuntu}-amd64.tar.gz"
    bundle = args.output / filename

    def exclude_receipts(info):
        return None if Path(info.name).name in (".installed", ".install.lock") else info

    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(metadata_path, arcname="runtime.json")
        archive.add(target, arcname="runtime", filter=exclude_receipts)
    with bundle.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    (args.output / "SHA256SUMS").write_text(f"{digest}  {filename}\n")
    # Exercise the same extraction path used by Salt, with no npm commands.
    shutil.copyfile(bundle, installer.CACHE / "paseo-runtime.tar.gz")
    node = installer.node_bin(artifacts)
    env = dict(os.environ, PATH=str(node) + ":" + os.environ["PATH"])
    restored = installer.ROOT / "paseo-runtime-verification"
    restored.mkdir()
    installer.install_paseo_runtime(
        dict(paseo, runtime={"hash": "sha256=" + digest}), restored, node, env
    )
    installer.run([str(restored / "bin/paseo"), "--version"], env)
    print(json.dumps({"archive": filename, "sha256": digest, "runtime": metadata}))


if __name__ == "__main__":
    main()
