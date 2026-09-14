#!/usr/bin/env python3
"""Create one Oduflow 1.75 production while its public ingress is closed.

Run as root with a root-owned 0600 JSON config path (default below). Optional
git_username/git_token_file install HTTPS credentials through Git's stdin helper.
Required config: instance_uuid, name, domain, repo_url, branch, team_id,
team_hostname, ui_password_file, admin_password_file. Optional: api_port=8000,
admin_login=admin. Image is fixed to odoo:19.0 and template_name is empty.

The caller must keep provider firewall ingress 80/443 closed for IPv4 AND IPv6
until status=hardened. Before invoking, it writes a root-owned 0600 guard at
/etc/oduflow/production-publication-guard.json containing instance_uuid,
domain, firewall_group_id and public_ingress_closed=true. This is an operator
attestation, not a substitute for verifying the actual provider firewall.
Remove the guard on publication. No firewall rules are changed by this helper.

Creation is claimed durably before POST. Uncertain POSTs never automatically
repeat; a matching existing production can be reconciled. Passwords travel only
in local API headers or docker exec stdin, never argv, receipts or log output.
"""

import base64
import fcntl
import hashlib
import hmac
import http.client
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import tomllib

GUARD = Path("/etc/oduflow/production-publication-guard.json")
STATE_DIR = Path("/var/lib/oduflow/production")
ODUFLOW_CONFIG = Path("/etc/oduflow/oduflow.toml")
MAX_BYTES = 1024 * 1024


class SafeError(Exception):
    pass


def private_read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size > 65536
            ):
                raise SafeError("private_file_permissions")
            return handle.read(65537).decode("utf-8")
    except (OSError, UnicodeError):
        raise SafeError("private_file_unavailable") from None


def private_json(path):
    try:
        value = json.loads(private_read(path))
    except ValueError:
        raise SafeError("invalid_private_json") from None
    if not isinstance(value, dict):
        raise SafeError("invalid_private_json")
    return value


def validate(config):
    patterns = {
        "name": r"[a-z0-9][a-z0-9-]{0,30}",
        "domain": r"[a-z0-9-]+(?:\.[a-z0-9-]+){2,}",
        "team_hostname": r"[a-z0-9-]+(?:\.[a-z0-9-]+){2,}",
        "team_id": r"[a-zA-Z0-9_-]{1,40}",
        "repo_url": r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+\.git",
    }
    for field, pattern in patterns.items():
        if not isinstance(config.get(field), str) or not re.fullmatch(pattern, config[field]):
            raise SafeError("invalid_production_config")
    try:
        if str(UUID(config.get("instance_uuid", ""))) != config.get("instance_uuid"):
            raise ValueError
    except (ValueError, AttributeError):
        raise SafeError("invalid_instance_uuid") from None
    branch = config.get("branch")
    if (
        not isinstance(branch, str)
        or not 1 <= len(branch) <= 255
        or branch.startswith("-")
        or re.search(r"[\x00-\x20\x7f]", branch)
    ):
        raise SafeError("invalid_branch")
    port = config.get("api_port", 8000)
    if type(port) is not int or not 1 <= port <= 65535:
        raise SafeError("invalid_api_port")
    if config.get("api_host", "127.0.0.1") not in ("127.0.0.1", "172.17.0.1"):
        raise SafeError("invalid_api_host")
    login = config.get("admin_login", "admin")
    if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,128}", login):
        raise SafeError("invalid_admin_login")
    for field in ("ui_password_file", "admin_password_file"):
        if not isinstance(config.get(field), str) or not config[field].startswith("/"):
            raise SafeError("invalid_secret_path")
    return config


def verify_runtime_config(config):
    """Check the configured single-client team instead of guessing its identity."""
    try:
        runtime = tomllib.loads(private_read(ODUFLOW_CONFIG))
        teams = runtime["team"]
        team = teams[config["team_id"]]
        server = runtime["server"]
        matches = (
            len(teams) == 1
            and team["hostname"] == config["team_hostname"]
            and server["bind"] == config.get("api_host", "127.0.0.1")
            and server["port"] == config.get("api_port", 8000)
            and runtime["storage"]["data_dir"] == "/srv/oduflow/data"
            and hmac.compare_digest(
                team["ui_password"].encode(),
                private_read(config["ui_password_file"]).rstrip("\n").encode(),
            )
        )
    except (KeyError, TypeError, ValueError):
        raise SafeError("oduflow_runtime_config_invalid") from None
    if not matches:
        raise SafeError("oduflow_runtime_config_mismatch")


def setup_git(config):
    if not config.get("git_token_file"):
        return  # Compatibility for callers with an already configured store.
    username = config.get("git_username")
    token = private_read(config["git_token_file"]).rstrip("\n")
    if (
        not isinstance(username, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", username)
        or not re.fullmatch(r"[!-~]{16,4096}", token)
    ):
        raise SafeError("invalid_git_credentials")
    directory = Path("/srv/oduflow/data") / ("team_" + config["team_id"])
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SafeError("unsafe_git_credentials_directory")
    credentials = directory / ".git-credentials"
    if credentials.exists() or credentials.is_symlink():
        private_read(credentials)  # Reject symlinks/non-private existing stores.
    environment = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "credential.helper",
        "GIT_CONFIG_VALUE_1": f"store --file {credentials}",
        "GIT_CONFIG_KEY_2": "http.followRedirects",
        "GIT_CONFIG_VALUE_2": "false",
    }
    try:
        result = subprocess.run(
            ["git", "credential", "approve"],
            input=f"protocol=https\nhost=github.com\nusername={username}\npassword={token}\n\n".encode(),
            env=environment,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise SafeError("git_credentials_store_failed")
        credentials.chmod(0o600)
        private_read(credentials)
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--heads", config["repo_url"], config["branch"]],
            env=environment,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise SafeError("git_repository_access_failed")
    except (OSError, subprocess.TimeoutExpired):
        raise SafeError("git_credentials_setup_failed") from None


def expected(config):
    return {key: config[key] for key in ("name", "domain", "repo_url", "branch")} | {
        "odoo_image": "odoo:19.0"
    }


def fingerprint(config):
    data = expected(config) | {
        "instance_uuid": config["instance_uuid"],
        "team_id": config["team_id"],
        "team_hostname": config["team_hostname"],
        "admin_login": config.get("admin_login", "admin"),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def verify_info(info, config):
    if any(info.get(key) != value for key, value in expected(config).items()):
        raise SafeError("production_metadata_mismatch")
    if info.get("extra_addons") or info.get("auto_update"):
        raise SafeError("production_metadata_mismatch")
    for field in ("odoo_container", "db_name"):
        if not isinstance(info.get(field), str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+", info[field]
        ):
            raise SafeError("invalid_production_identity")
    return info


class LocalAPI:
    def __init__(self, config):
        self.config = config
        password = private_read(config["ui_password_file"]).rstrip("\n")
        if not password:
            raise SafeError("missing_ui_password")
        self.authorization = "Basic " + base64.b64encode(("admin:" + password).encode()).decode()

    def request(self, method, path, data=None):
        connection = http.client.HTTPConnection(
            self.config.get("api_host", "127.0.0.1"), self.config.get("api_port", 8000), timeout=900
        )
        try:
            connection.request(
                method,
                path,
                body=None if data is None else json.dumps(data),
                headers={
                    "Authorization": self.authorization,
                    "Host": self.config["team_hostname"],
                    "Content-Type": "application/json",
                    # Oduflow's browser CSRF middleware checks unsafe request origins.
                    "Origin": "http://" + self.config["team_hostname"],
                },
            )
            response = connection.getresponse()
            if response.status == 404 and method == "GET":
                return None
            if response.status != 200:
                raise SafeError("local_api_http_" + str(response.status))
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise SafeError("local_api_response_too_large")
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise SafeError("local_api_invalid_response")
            return result
        except (OSError, http.client.HTTPException, ValueError):
            raise SafeError(
                "local_api_outcome_unknown" if method == "POST" else "local_api_unavailable"
            ) from None
        finally:
            connection.close()

    def info(self):
        return self.request("GET", "/api/productions/" + self.config["name"])

    def create(self):
        return self.request(
            "POST",
            "/api/productions/create",
            expected(self.config)
            | {
                "template_name": "",
                "auto_update": False,
            },
        )


def verify_guard(config):
    guard = private_json(GUARD)
    if (
        guard.get("instance_uuid") != config["instance_uuid"]
        or guard.get("domain") != config["domain"]
        or guard.get("public_ingress_closed") is not True
        or not isinstance(guard.get("firewall_group_id"), str)
        or not guard["firewall_group_id"]
    ):
        raise SafeError("publication_guard_missing_or_mismatched")


def save_state(path, data):
    temporary = path.with_name(path.name + "." + uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def docker_run(args, *, input=None):
    try:
        result = subprocess.run(
            ["docker", *args], input=input, capture_output=True, timeout=180, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SafeError("docker_operation_failed") from None
    if result.returncode:
        raise SafeError("docker_operation_failed")
    return result.stdout


def verify_container(info, config, known_container_id=None):
    """Verify ownership and the immutable ID, including completed reruns."""
    try:
        container = json.loads(docker_run(["inspect", info["odoo_container"]]))[0]
    except (ValueError, IndexError, TypeError):
        raise SafeError("docker_identity_invalid") from None
    labels = container.get("Config", {}).get("Labels", {})
    required = {
        "oduflow.managed": "true",
        "oduflow.team": config["team_id"],
        "oduflow.prod": "true",
        "oduflow.prod_name": config["name"],
        "oduflow.domain": config["domain"],
        "oduflow.repo": config["repo_url"],
        "oduflow.git_branch": config["branch"],
        "oduflow.image": "odoo:19.0",
    }
    container_id = container.get("Id")
    if (
        any(labels.get(key) != value for key, value in required.items())
        or not isinstance(container_id, str)
        or not re.fullmatch(r"[a-f0-9]{64}", container_id)
        or (known_container_id and container_id != known_container_id)
    ):
        raise SafeError("docker_identity_mismatch")
    return container_id


def harden(info, config, password, known_container_id=None):
    container_id = verify_container(info, config, known_container_id)
    # No DB or admin password in argv. libpq reads PGPASSWORD. Use the exact
    # inspected container ID so a concurrent name replacement cannot redirect exec.
    launcher = (
        "import os,sys; os.environ['PGPASSWORD']=os.environ['PASSWORD']; "
        "os.execv('/usr/bin/odoo',['odoo','shell','--no-http','--stop-after-init',"
        "'--log-level=critical','--db_host='+os.environ['HOST'],"
        "'--db_user='+os.environ['USER'],'--database='+sys.argv[1]])"
    )
    payload = repr({"login": config.get("admin_login", "admin"), "password": password})
    script = (
        "admin = env.ref('base.user_admin').sudo()\n"
        "assert admin.active and not admin.share\n"
        f"admin.write({payload})\n"
        "env.cr.commit()\n"
        "print('ODUFLOW_ADMIN_HARDENED')\n"
    )
    output = docker_run(
        ["exec", "-i", "--user", "odoo", container_id, "python3", "-c", launcher, info["db_name"]],
        input=script.encode(),
    )
    if b"ODUFLOW_ADMIN_HARDENED\n" not in output:
        raise SafeError("admin_password_commit_unconfirmed")
    return container_id


def execute(config, api, path):
    digest = fingerprint(config)
    state = private_json(path) if path.exists() or path.is_symlink() else None
    if state and state.get("fingerprint") != digest:
        raise SafeError("production_receipt_mismatch")
    info = api.info()
    if info is not None:
        verify_info(info, config)
        if not state:
            raise SafeError("production_not_dispatched")
    if state and state.get("status") == "hardened":
        if info is None:
            raise SafeError("known_production_missing")
        known_container_id = state.get("container_id")
        if not isinstance(known_container_id, str) or not re.fullmatch(
            r"[a-f0-9]{64}", known_container_id
        ):
            raise SafeError("production_receipt_missing_container")
        verify_container(info, config, known_container_id)
        return {"changed": False, "status": "hardened", "domain": config["domain"]}
    verify_guard(config)
    password = private_read(config["admin_password_file"]).rstrip("\n")
    if len(password) < 24 or len(password) > 1024 or len(set(password)) < 8:
        raise SafeError("admin_password_too_weak")
    if info is None:
        if state:
            raise SafeError("production_create_outcome_unknown")
        setup_git(config)
        state = {"fingerprint": digest, "status": "claimed"}
        save_state(path, state)
        api.create()
        info = api.info()
        if info is None:
            raise SafeError("production_create_outcome_unknown")
        verify_info(info, config)
    if info.get("container_status") != "running" or info.get("deploy_in_progress"):
        raise SafeError("production_not_ready")
    container_id = harden(info, config, password, state.get("container_id"))
    save_state(path, {"fingerprint": digest, "status": "hardened", "container_id": container_id})
    return {"changed": True, "status": "hardened", "domain": config["domain"]}


def main():
    if os.geteuid() != 0:
        raise SafeError("root_required")
    config = validate(
        private_json(sys.argv[1] if len(sys.argv) > 1 else "/etc/oduflow/production.json")
    )
    verify_runtime_config(config)
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = STATE_DIR.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SafeError("private_state_directory_required")
    lock = os.open(STATE_DIR / "lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        print(json.dumps(execute(config, LocalAPI(config), STATE_DIR / "receipt.json")))
    finally:
        os.close(lock)


if __name__ == "__main__":
    try:
        main()
    except SafeError as error:
        print(json.dumps({"changed": False, "error": str(error)}))
        sys.exit(1)
    except Exception:
        print(json.dumps({"changed": False, "error": "production_helper_failed"}))
        sys.exit(1)
