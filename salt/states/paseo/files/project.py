#!/usr/bin/python3
"""Register the client's existing GitHub repository without resetting user work."""

import fcntl
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path("/srv/oduflow/data/paseo")
CONFIG = Path("/etc/paseo/project.json")


class SafeError(Exception):
    pass


def run(argv, *, env=None, cwd=None):
    try:
        result = subprocess.run(
            argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=240, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SafeError("paseo_project_command_failed") from None
    if result.returncode:
        raise SafeError("paseo_project_command_failed")
    return result.stdout.strip()


def private_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise SafeError("paseo_project_private_config_invalid")
        return stream.read(1048576)


def validate_config(config):
    patterns = {
        "instance_uuid": r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "device": r"/dev/disk/by-id/[A-Za-z0-9_.:+-]+",
        "repo": r"[A-Za-z0-9-]+/[A-Za-z0-9_][A-Za-z0-9_.-]*",
    }
    if not isinstance(config, dict) or any(
        not isinstance(config.get(key), str) or not re.fullmatch(pattern, config[key])
        for key, pattern in patterns.items()
    ):
        raise SafeError("paseo_project_config_invalid")
    branch = config.get("branch")
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        raise SafeError("paseo_project_branch_invalid")
    run(["git", "check-ref-format", "--branch", branch])
    return config


def check_directory(path, uid):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
        raise SafeError("paseo_project_directory_unsafe")


def checkout(config, parent, env):
    name = config["repo"].split("/")[1]
    target = parent / name
    url = "https://github.com/" + config["repo"] + ".git"
    if target.exists() or target.is_symlink():
        check_directory(target, os.getuid())
        if not (target / ".git").is_dir() or (target / ".git").is_symlink():
            raise SafeError("paseo_project_checkout_conflict")
        origin = run(["git", "-C", str(target), "config", "--get", "remote.origin.url"], env=env)
        if origin.removesuffix(".git") != url.removesuffix(".git"):
            raise SafeError("paseo_project_repository_mismatch")
        return target, False
    temporary = Path(tempfile.mkdtemp(prefix=".oduflow-clone-", dir=parent))
    try:
        run(["git", "clone", "--branch", config["branch"], "--", url, str(temporary)], env=env)
        temporary.rename(target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return target, True


def ensure_project(cli, target, env):
    def projects():
        rows = json.loads(
            run([str(cli), "project", "ls", "--home", "/srv/paseo", "--json"], env=env)
        )
        if not isinstance(rows, list):
            raise SafeError("paseo_project_list_invalid")
        return [row for row in rows if Path(row["path"]).resolve() == target.resolve()]

    matches = projects()
    if not matches:
        run([str(cli), "project", "create", str(target), "--home", "/srv/paseo", "--json"], env=env)
        matches = projects()
        if len(matches) != 1:
            raise SafeError("paseo_project_registration_failed")
        return True
    if len(matches) != 1:
        raise SafeError("paseo_project_duplicate")
    return False


def provision(config):
    user = pwd.getpwnam("paseo")
    run(
        [
            "/usr/local/libexec/oduflow-storage",
            "--instance-uuid",
            config["instance_uuid"],
            "--device",
            config["device"],
            "--verify",
        ]
    )
    check_directory(HOME, user.pw_uid)
    if Path("/srv/paseo").resolve() != HOME:
        raise SafeError("paseo_project_home_mismatch")
    for directory in (HOME / ".config", HOME / ".config/gh"):
        if directory.exists() or directory.is_symlink():
            check_directory(directory, user.pw_uid)
    if "--check" in sys.argv:
        return False
    manifest = json.loads(Path("/var/cache/oduflow-apps/artifacts.json").read_text())
    paseo, node = manifest["paseo"], manifest["node"]
    if (
        not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", paseo["version"])
        or not re.fullmatch(r"[0-9a-f]{40}", paseo["commit"])
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", node["version"])
    ):
        raise SafeError("paseo_project_runtime_invalid")
    cli = Path("/opt/oduflow/paseo") / (paseo["version"] + "+" + paseo["commit"][:12]) / "bin/paseo"
    node_bin = f"/opt/oduflow/node/{node['version']}/node-v{node['version']}-linux-x64/bin"
    password = dict(
        line.split("=", 1)
        for line in private_read("/etc/paseo/credentials.env").splitlines()
        if "=" in line
    )["PASEO_PASSWORD"]
    env = {
        "HOME": "/srv/paseo",
        "PASEO_HOME": "/srv/paseo",
        "PASEO_PASSWORD": password,
        "PATH": node_bin + ":/usr/local/bin:/usr/bin:/bin",
        "GIT_TERMINAL_PROMPT": "0",
        "GH_PROMPT_DISABLED": "1",
    }
    os.initgroups("paseo", user.pw_gid)
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)
    os.umask(0o077)
    os.chdir(HOME)
    # Configure only GitHub's credential helper; preserve other user Git settings.
    key = "credential.https://github.com.helper"
    result = subprocess.run(
        ["git", "config", "--global", "--get-all", key], env=env, capture_output=True, text=True
    )
    changed = result.stdout.splitlines() != ["", "!/usr/bin/gh auth git-credential"]
    if changed:
        run(["git", "config", "--global", "--replace-all", key, ""], env=env)
        run(
            ["git", "config", "--global", "--add", key, "!/usr/bin/gh auth git-credential"], env=env
        )
    parent = HOME / "projects"
    parent.mkdir(mode=0o700, exist_ok=True)
    check_directory(parent, user.pw_uid)
    target, cloned = checkout(config, parent, env)
    registered = ensure_project(cli, target, env)
    return changed or cloned or registered


def main():
    try:
        # Serialize concurrent/retried provisioning without caching secret results.
        fd = os.open(
            "/run/oduflow-paseo-project.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            changed = provision(validate_config(json.loads(private_read(CONFIG))))
        print(json.dumps({"changed": changed, "comment": "paseo_project_ready"}))
        return 0
    except SafeError as error:
        code = str(error)
    except Exception:
        code = "paseo_project_failed"
    print(json.dumps({"changed": False, "comment": code}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
