#!/usr/bin/python3
"""Install pinned application artifacts; emit a receipt only after success."""

import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path("/opt/oduflow")
CACHE = Path("/var/cache/oduflow-apps")
MANIFEST = CACHE / "artifacts.json"
APPS = ("oduflow", "paseo")
EXECUTABLES = {"oduflow": "bin/oduflow", "paseo": "bin/paseo"}
SERVER_ENTRY = "lib/node_modules/@getpaseo/server/dist/scripts/supervisor-entrypoint.js"
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY = re.compile(r"https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\.git\Z")
PACKAGE = re.compile(r"@getpaseo/[a-z][a-z-]*\Z")


def node_bin(manifest):
    version = manifest["node"]["version"]
    return ROOT / "node" / version / ("node-v" + version + "-linux-x64/bin")


def uv_bin(manifest):
    return ROOT / "uv" / manifest["uv"]["version"] / "uv-x86_64-unknown-linux-gnu/uv"


def release(app, spec):
    # A source build is identified by its commit: the fork keeps the upstream
    # version string, so the version alone cannot describe what is installed.
    if app == "paseo":
        return spec["version"] + "+" + spec["commit"][:12]
    return spec["version"]


def run(command, env, cwd=None):
    subprocess.run(command, env=env, cwd=cwd, check=True, stdout=sys.stderr, umask=0o022)


def install_oduflow(spec, target, uv, env):
    env = dict(env, UV_TOOL_DIR=str(target / "tools"), UV_TOOL_BIN_DIR=str(target / "bin"))
    run(
        [
            str(uv),
            "tool",
            "install",
            "--python",
            "/usr/bin/python3",
            "--no-managed-python",
            "--force",
            str(CACHE / ("oduflow-" + spec["version"] + "-py3-none-any.whl")),
        ],
        env,
    )


def checkout(spec, source, env):
    if not REPOSITORY.match(spec["repository"]) or not COMMIT.match(spec["commit"]):
        raise ValueError("Paseo source must pin a full commit of an HTTPS GitHub repository")
    source.mkdir(parents=True)
    if spec.get("archive"):
        archive_path = CACHE / ("paseo-" + spec["commit"] + ".tar.gz")
        with archive_path.open("rb") as stream:
            digest = "sha256=" + hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != spec["archive"]["hash"]:
            raise RuntimeError("Paseo source archive checksum mismatch")
        with tarfile.open(archive_path, "r:gz") as archive:
            if archive.pax_headers.get("comment") != spec["commit"]:
                raise RuntimeError("Paseo source archive commit mismatch")
            archive.extractall(source, filter="data")
    else:
        git = ["git", "-C", str(source)]
        run(git + ["init", "--quiet"], env)
        run(git + ["remote", "add", "origin", spec["repository"]], env)
        run(git + ["fetch", "--quiet", "--depth", "1", "origin", spec["commit"]], env)
        run(git + ["checkout", "--quiet", "--detach", "FETCH_HEAD"], env)
        head = subprocess.run(
            git + ["rev-parse", "HEAD"], env=env, check=True, capture_output=True, text=True
        )
        if head.stdout.strip() != spec["commit"]:
            raise RuntimeError("Checked-out Paseo source does not match the pinned commit")
    manifest = json.loads((source / "package.json").read_text())
    if manifest.get("version") != spec["version"]:
        raise RuntimeError("Paseo source does not carry the pinned version")
    # Contributor Git hooks are not installed on a client; the upstream container
    # build drops the same script before resolving dependencies.
    manifest.get("scripts", {}).pop("prepare", None)
    (source / "package.json").write_text(json.dumps(manifest) + "\n")


def install_paseo(spec, target, node, env):
    packages = spec["packages"]
    if not packages or not all(PACKAGE.match(name) for name in packages):
        raise ValueError("Paseo source must pin its own workspace package names")
    source = CACHE / "paseo-src"
    packs = CACHE / "paseo-packs"
    npm = str(node / "npm")
    for directory in (source, packs):
        shutil.rmtree(directory, ignore_errors=True)
    try:
        checkout(spec, source, env)
        packs.mkdir(parents=True)
        # Speech runtime downloads and update checks must not reach outside the
        # pinned dependency set; the cache lives and dies with this build.
        build = dict(
            env,
            # V8 sizes its default heap from RAM, excluding swap. The pinned
            # TypeScript server build exceeds the default on a 2 GB client.
            NODE_OPTIONS="--max-old-space-size=4096",
            ONNXRUNTIME_NODE_INSTALL="skip",
            npm_config_cache=str(source / ".npm"),
            npm_config_update_notifier="false",
        )
        run([npm, "ci", "--no-audit", "--no-fund"], build, cwd=source)
        # Each workspace is packed in dependency order: packing builds it and the
        # next package consumes the freshly built output.
        for name in packages:
            run(
                [npm, "pack", "--workspace", name, "--pack-destination", str(packs)],
                build,
                cwd=source,
            )
        tarballs = sorted(str(path) for path in packs.glob("*.tgz"))
        if len(tarballs) != len(packages):
            raise RuntimeError("Paseo packaging did not produce one tarball per pinned package")
        run(
            [npm, "install", "--global", "--prefix", str(target), "--no-audit", "--no-fund"]
            + tarballs,
            build,
        )
    finally:
        for directory in (source, packs):
            shutil.rmtree(directory, ignore_errors=True)
    entry = target / SERVER_ENTRY
    if not entry.is_file():
        raise RuntimeError("Paseo daemon supervisor entrypoint missing after installation")
    run([str(node / "node"), "--check", str(entry)], env)


def install_paseo_runtime(spec, target, node, env):
    runtime = spec["runtime"]
    if not re.fullmatch(r"sha256=[0-9a-f]{64}", runtime.get("hash", "")):
        raise ValueError("Paseo runtime needs a SHA256 checksum")
    archive_path = CACHE / "paseo-runtime.tar.gz"
    with archive_path.open("rb") as stream:
        digest = "sha256=" + hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != runtime["hash"]:
        raise RuntimeError("Paseo runtime checksum mismatch")
    host = platform.freedesktop_os_release()
    expected = {
        "contract": 1,
        "os": host["ID"],
        "os_version": host["VERSION_ID"],
        "architecture": platform.machine(),
        "node_version": node.parent.name.removeprefix("node-v").removesuffix("-linux-x64"),
        "version": spec["version"],
        "commit": spec["commit"],
    }
    # Verify compatibility before touching the installation, including on retries.
    with tarfile.open(archive_path, "r:gz") as archive:
        metadata = archive.extractfile("runtime.json")
        if metadata is None or json.load(metadata) != expected:
            raise RuntimeError("Paseo runtime does not match this host and pinned release")
        members = archive.getmembers()
        if any(
            item.name not in ("runtime.json", "runtime") and not item.name.startswith("runtime/")
            for item in members
        ):
            raise RuntimeError("Unexpected Paseo runtime archive path")
        stage = CACHE / "paseo-runtime-stage"
        shutil.rmtree(stage, ignore_errors=True)
        try:
            stage.mkdir()
            archive.extractall(stage, filter="data")
            if (
                not (stage / "runtime" / SERVER_ENTRY).is_file()
                or not (stage / "runtime/bin/paseo").exists()
            ):
                raise RuntimeError("Incomplete Paseo runtime")
            for child in target.iterdir():
                if child.name == ".install.lock":
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            shutil.copytree(stage / "runtime", target, dirs_exist_ok=True, symlinks=True)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    run([str(node / "node"), "--check", str(target / SERVER_ENTRY)], env)


def install(app):
    manifest = json.loads(MANIFEST.read_text())
    spec = manifest[app]
    node = node_bin(manifest)
    version = release(app, spec)
    target = ROOT / app / version
    for directory in (ROOT, ROOT / app, target):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)
    with (target / ".install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = target / ".installed"
        executable = target / EXECUTABLES[app]
        if receipt.exists() and executable.exists():
            return False
        env = dict(os.environ)
        env["PATH"] = str(node) + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        if app == "oduflow":
            install_oduflow(spec, target, uv_bin(manifest), env)
        elif spec.get("install_method", "source") == "prebuilt":
            install_paseo_runtime(spec, target, node, env)
        elif spec.get("install_method", "source") == "source":
            install_paseo(spec, target, node, env)
        else:
            raise ValueError("Unknown Paseo installation method")
        if not executable.exists():
            raise RuntimeError("Application executable missing after installation")
        receipt.write_text(version + "\n")
        return True


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in APPS:
        raise SystemExit("Usage: oduflow-install-app oduflow|paseo")
    try:
        changed = install(sys.argv[1])
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        tarfile.TarError,
        subprocess.CalledProcessError,
    ):
        raise SystemExit("Application installation failed; no completion receipt written")
    print(
        json.dumps(
            {
                "changed": changed,
                "comment": "Pinned application installed" if changed else "Already installed",
            }
        )
    )
