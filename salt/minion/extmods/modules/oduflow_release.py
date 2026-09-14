"""Check out an exact client revision and apply its local states with private pillar."""

import json
import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

__virtualname__ = "oduflow_release"
__salt__ = {}
__opts__ = {}
ROOT = Path("/opt/oduflow/client/releases")
REPOSITORY = "https://github.com/oduflow/oduflow-client.git"
_OWNER_UID = 0
_RUN_ROOT = "/run"


def __virtual__():
    return __virtualname__


def revision(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("client_revision_invalid")
    return value


def _trusted(path, directory=True):
    info = path.lstat()
    if (
        info.st_uid not in (0, _OWNER_UID)
        or (info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))
        or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    ):
        raise ValueError("client_repository_path_unsafe")


def _git(path, *args):
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith("GIT_"):
            del environment[key]
    environment.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
    )
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(path), *args],
        env=environment,
        capture_output=True,
        timeout=300,
        check=False,
    )
    if result.returncode:
        raise ValueError("client_repository_git_failed")
    return result.stdout.decode().strip()


def checkout(commit):
    """Never reset a dirty release or execute a branch name supplied over RPC."""
    commit = revision(commit)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o755)
    for parent in (ROOT, *ROOT.parents):
        _trusted(parent)
    target = ROOT / commit
    if target.exists() or target.is_symlink():
        _trusted(target)
        _trusted(target / ".git")
        if _git(target, "rev-parse", "HEAD") != commit or _git(
            target, "status", "--porcelain", "--untracked-files=all"
        ):
            raise ValueError("client_release_checkout_modified")
    else:
        # An interrupted fetch never becomes a release. A later request may retry
        # read-only Git operations in a fresh staging directory.
        with tempfile.TemporaryDirectory(prefix=".fetch-", dir=ROOT) as stage:
            staging = Path(stage)
            _git(staging, "init", "--quiet")
            _git(staging, "fetch", "--quiet", "--depth=1", REPOSITORY, commit)
            _git(staging, "checkout", "--quiet", "--detach", "FETCH_HEAD")
            if _git(staging, "rev-parse", "HEAD") != commit:
                raise ValueError("client_revision_mismatch")
            os.rename(staging, target)
    manifest = json.loads((target / "release.json").read_text())
    if manifest.get("contract") != 1 or manifest.get("salt") != "3006.27":
        raise ValueError("client_release_contract_unsupported")
    return str(target)


@contextmanager
def _tree_options(target, minion_id, pillar=None):
    # Fetch from the authenticated master before switching the file client to
    # local mode. Pillar stays in memory and is never written into the checkout.
    pillar = __salt__["pillar.items"]() if pillar is None else pillar
    if not isinstance(pillar, dict) or "client-" + pillar.get("instance_uuid", "") != minion_id:
        raise ValueError("client_release_pillar_identity_invalid")
    with tempfile.TemporaryDirectory(prefix="oduflow-release-", dir=_RUN_ROOT) as directory:
        config = Path(directory) / "minion.json"
        config.write_text(
            json.dumps(
                {
                    "file_client": "local",
                    "file_roots": {
                        "base": [str(target / "salt/states"), str(target / "salt/minion/extmods")]
                    },
                    "pillar_roots": {"base": []},
                    "ext_pillar": [],
                    "minion_pillar_cache": False,
                    "pillar_cache": False,
                    "cache_jobs": False,
                    "state_events": False,
                }
            )
        )
        config.chmod(0o600)
        yield {"localconfig": str(config), "pillar": pillar}


def options(commit, minion_id):
    """Internal context manager used only by the durable job executor."""
    return local_options(commit, minion_id)


@contextmanager
def local_options(commit, minion_id):
    pillar = __salt__["pillar.items"]()
    if not isinstance(pillar, dict) or "client-" + pillar.get("instance_uuid", "") != minion_id:
        raise ValueError("client_release_pillar_identity_invalid")
    with _tree_options(Path(checkout(commit)), minion_id, pillar) as value:
        yield value


@contextmanager
def sources_options(source_json, minion_id):
    """Apply an addressed infrastructure bundle without a public platform fileserver."""
    if not isinstance(source_json, str) or len(source_json.encode()) > 524288:
        raise ValueError("infrastructure_sources_invalid")
    sources = json.loads(source_json)
    if not isinstance(sources, dict) or not 1 <= len(sources) <= 200:
        raise ValueError("infrastructure_sources_invalid")
    with tempfile.TemporaryDirectory(prefix="oduflow-sources-", dir=_RUN_ROOT) as directory:
        root = Path(directory)
        states = root / "salt/states"
        for name, content in sources.items():
            path = Path(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or not isinstance(content, str)
                or "\x00" in name
            ):
                raise ValueError("infrastructure_sources_invalid")
            target = states / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        with _tree_options(root, minion_id) as value:
            yield value
