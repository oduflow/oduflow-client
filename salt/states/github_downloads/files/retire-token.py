#!/usr/bin/env python3
"""Retire managed GitHub HTTPS credentials after repository-scoped SSH enrollment."""

import json
import os
import stat
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

STORE = Path("/srv/oduflow/data/team_1/.git-credentials")
TOKEN = Path("/etc/oduflow/production-git-token")


def private(path):
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError("unsafe_credentials_path")
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe_credentials_file")
    return path.read_text()


def retire():
    original = private(STORE)
    token = private(TOKEN)
    changed = False
    if original is not None:
        retained = "".join(
            line
            for line in original.splitlines(keepends=True)
            if urlsplit(line.strip()).hostname != "github.com"
        )
        if retained != original:
            fd, name = tempfile.mkstemp(prefix=".credentials-", dir=STORE.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    stream.write(retained)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, STORE)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            changed = True
    if token is not None:
        TOKEN.unlink()
        changed = True
    return changed


if __name__ == "__main__":
    try:
        print(json.dumps({"changed": retire(), "comment": "github_https_credentials_retired"}))
    except (OSError, ValueError):
        print(json.dumps({"changed": False, "comment": "github_credential_retirement_failed"}))
        raise SystemExit(1) from None
