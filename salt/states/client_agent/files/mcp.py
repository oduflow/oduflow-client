#!/usr/bin/python3
"""Configure client MCP for optional agents, preserving unrelated user settings."""

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

TOKEN_ENV = "ODUFLOW_MCP_TOKEN"


def check_path(path, directory=False):
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Unsafe agent configuration path")


def claude_config(path, url):
    check_path(path)
    original = json.loads(path.read_text()) if path.exists() else {}
    desired = {
        "type": "http",
        "url": url,
        "headers": {"Authorization": "Bearer ${ODUFLOW_MCP_TOKEN}"},
    }
    servers = original.setdefault("mcpServers", {})
    if servers.get("oduflow") == desired:
        return False
    servers["oduflow"] = desired
    fd, temporary = tempfile.mkstemp(prefix=".claude-mcp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(original, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def codex_config(home, url, binary="/usr/local/bin/codex"):
    directory = home / ".codex"
    check_path(directory, directory=True)
    directory.mkdir(mode=0o700, exist_ok=True)
    check_path(directory / "config.toml")
    env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(directory)}

    def run(*args):
        result = subprocess.run(
            [binary, "mcp", *args], env=env, capture_output=True, text=True, timeout=60
        )
        if result.returncode:
            raise ValueError("Codex MCP configuration command failed")
        return result.stdout

    # Let the CLI parse and edit TOML without rewriting unrelated settings.
    servers = json.loads(run("list", "--json"))
    desired = {"type": "streamable_http", "url": url, "bearer_token_env_var": TOKEN_ENV}
    for server in servers:
        if server.get("name") == "oduflow" and server.get("enabled", True):
            transport = server.get("transport", {})
            if all(transport.get(key) == value for key, value in desired.items()):
                return False
    run("add", "oduflow", "--url", url, "--bearer-token-env-var", TOKEN_ENV)
    (directory / "config.toml").chmod(0o600)
    return True


def configure(home, url):
    if not re.fullmatch(r"https://[a-z0-9][a-z0-9.-]*/mcp", url):
        raise ValueError("Invalid client MCP endpoint")
    check_path(home, directory=True)
    if not home.is_dir():
        raise ValueError("Paseo home is missing")
    changed = codex_config(home, url)
    return claude_config(home / ".claude.json", url) or changed


if __name__ == "__main__":
    try:
        changed = configure(Path("/srv/oduflow/data/paseo"), sys.argv[1])
        print(json.dumps({"changed": changed, "comment": "Client agent MCP configured"}))
    except Exception:
        # CLI output and existing user files may contain credentials.
        print("Client agent MCP configuration failed", file=sys.stderr)
        sys.exit(1)
