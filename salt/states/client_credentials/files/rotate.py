#!/usr/bin/env python3
"""Rotate one owned client's login credentials without recreating its production.

Desired secrets are supplied in a root-only file, never command arguments.
A durable revision binds retries to the same desired values. Partial application
is reconciled through authenticated probes before any repeat service restart.
"""

import base64
import copy
import fcntl
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import tomllib

CONFIG = Path("/etc/oduflow/credentials-desired.json")
STATE_DIR = Path("/var/lib/oduflow/credentials")
RUNTIME_CONFIG = Path("/etc/oduflow/oduflow.toml")
PASEO_ENV = Path("/etc/paseo/credentials.env")
PRODUCTION_CONFIG = Path("/etc/oduflow/production.json")
PRODUCTION_RECEIPT = Path("/var/lib/oduflow/production/receipt.json")
UI_PASSWORD = Path("/etc/oduflow/production-ui-password")
ADMIN_PASSWORD = Path("/etc/oduflow/production-admin-password")
STORAGE_RECEIPT = Path("/etc/oduflow/storage.json")
MAX_BYTES = 1024 * 1024
SECRET_NAMES = ("ui_password", "auth_token", "paseo_password", "production_admin_password")


class SafeError(Exception):
    pass


def private_read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size > MAX_BYTES
            ):
                raise SafeError("credential_file_permissions")
            return stream.read(MAX_BYTES + 1).decode()
    except (OSError, UnicodeError):
        raise SafeError("credential_file_unavailable") from None


def private_json(path):
    try:
        value = json.loads(private_read(path))
    except ValueError:
        raise SafeError("credential_configuration_invalid") from None
    if not isinstance(value, dict):
        raise SafeError("credential_configuration_invalid")
    return value


def atomic_write(path, content):
    if path.exists() or path.is_symlink():
        current = private_read(path)
        if hmac.compare_digest(current.encode(), content.encode()):
            return False
    temporary = path.with_name(path.name + "." + uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def save(path, value):
    atomic_write(path, json.dumps(value, sort_keys=True) + "\n")


def trusted_helper(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SafeError("credential_helper_untrusted")
    module = types.ModuleType("credential_dependency")
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


def validate(config):
    try:
        for key in ("instance_uuid", "request_id"):
            if str(UUID(config[key])) != config[key]:
                raise ValueError
        if type(config["revision"]) is not int or config["revision"] < 1:
            raise ValueError
        if not re.fullmatch(r"/dev/disk/by-id/[A-Za-z0-9_.:+-]+", config["device"]):
            raise ValueError
        for key in SECRET_NAMES:
            if not isinstance(config[key], str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{24,256}", config[key]
            ):
                raise ValueError
        for key in ("oduflow_hostname", "paseo_hostname", "production_domain"):
            if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+){2,}", config[key]):
                raise ValueError
    except (KeyError, ValueError, TypeError, AttributeError):
        raise SafeError("credential_configuration_invalid") from None
    return config


def verify_storage(config, storage):
    owner = private_json(STORAGE_RECEIPT)
    if (
        owner.get("instance_uuid") != config["instance_uuid"]
        or owner.get("device") != config["device"]
        or not owner.get("filesystem_uuid")
    ):
        raise SafeError("credential_storage_identity_mismatch")
    storage.prepare(
        SimpleNamespace(
            instance_uuid=config["instance_uuid"],
            device=config["device"],
            filesystem_uuid=owner["filesystem_uuid"],
            mount="/srv/oduflow/data",
            verify=True,
            allow_format=False,
            timeout=0,
        )
    )


def patch_runtime(original, config):
    try:
        parsed = tomllib.loads(original)
        if (
            set(parsed["team"]) != {"1"}
            or parsed["team"]["1"]["hostname"] != config["oduflow_hostname"]
            or parsed["server"]["host"] != "172.17.0.1"
            or parsed["server"]["port"] != 8000
            or parsed["storage"]["data_dir"] != "/srv/oduflow/data"
            or parsed["route"]["paseo"]["host"] != config["paseo_hostname"]
        ):
            raise ValueError
        expected = copy.deepcopy(parsed)
        expected["team"]["1"].update({key: config[key] for key in ("ui_password", "auth_token")})
        lines, section, count = [], None, {"ui_password": 0, "auth_token": 0}
        for line in original.splitlines(keepends=True):
            if line.strip().startswith("["):
                section = line.strip()
            replaced = False
            if section == "[team.1]":
                for key in count:
                    if re.match(r"\s*" + key + r"\s*=", line):
                        lines.append(f"{key} = {json.dumps(config[key])}\n")
                        count[key] += 1
                        replaced = True
            if not replaced:
                lines.append(line)
        result = "".join(lines)
        if set(count.values()) != {1} or tomllib.loads(result) != expected:
            raise ValueError
        return result
    except (ValueError, KeyError, TypeError):
        raise SafeError("credential_runtime_identity_mismatch") from None


def patch_paseo(original, password, auth_token):
    replacements = {"PASEO_PASSWORD": password, "ODUFLOW_MCP_TOKEN": auth_token}
    counts = dict.fromkeys(replacements, 0)
    lines = []
    for line in original.splitlines(keepends=True):
        key = line.split("=", 1)[0]
        if key in replacements:
            lines.append(key + "=" + replacements[key] + "\n")
            counts[key] += 1
        else:
            lines.append(line)
    if counts["PASEO_PASSWORD"] != 1 or counts["ODUFLOW_MCP_TOKEN"] > 1:
        raise SafeError("credential_paseo_configuration_invalid")
    if not counts["ODUFLOW_MCP_TOKEN"]:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append("ODUFLOW_MCP_TOKEN=" + auth_token + "\n")
    return "".join(lines)


def request(service, config, path, authorization=None, data=None):
    target = ("172.17.0.1", 8000) if service == "oduflow" else ("127.0.0.1", 6767)
    connection = http.client.HTTPConnection(*target, timeout=5)
    try:
        headers = {"Host": config[service + "_hostname"]}
        if authorization:
            headers["Authorization"] = authorization
        if data is not None:
            headers.update(
                {
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                }
            )
        connection.request(
            "POST" if data is not None else "GET",
            path,
            body=json.dumps(data) if data is not None else None,
            headers=headers,
        )
        response = connection.getresponse()
        # Authentication failure bodies are never propagated or logged.
        if response.status != 200:
            return response.status, b""
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise SafeError("credential_probe_response_too_large")
        return response.status, body
    except (OSError, http.client.HTTPException):
        raise SafeError("credential_probe_unavailable") from None
    finally:
        connection.close()


def service_ready(service, config):
    try:
        if service == "paseo":
            return (
                request(service, config, "/api/status")[0] == 401
                and request(service, config, "/api/status", "Bearer " + config["paseo_password"])[0]
                == 200
            )
        basic = "Basic " + base64.b64encode(("admin:" + config["ui_password"]).encode()).decode()
        if request(service, config, "/api/productions")[0] != 401:
            return False
        if request(service, config, "/api/productions", basic)[0] != 200:
            return False
        initialization = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "oduflow-credential-check", "version": "1"},
            },
        }
        if request(service, config, "/mcp", data=initialization)[0] != 401:
            return False
        status, body = request(
            service, config, "/mcp", "Bearer " + config["auth_token"], initialization
        )
        if status != 200:
            return False
        if body.startswith(b"event:") or body.startswith(b"data:"):
            body = next(line[5:].strip() for line in body.splitlines() if line.startswith(b"data:"))
        result = json.loads(body)
        return (
            isinstance(result.get("result"), dict)
            and bool(result["result"].get("protocolVersion"))
            and not result.get("error")
        )
    except (SafeError, ValueError, TypeError, AttributeError, StopIteration):
        return False


def ensure_service(service, config):
    if service not in ("oduflow", "paseo"):
        raise SafeError("credential_service_invalid")
    if service_ready(service, config):
        return False
    try:
        result = subprocess.run(
            ["systemctl", "restart", service], capture_output=True, timeout=180, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SafeError("credential_service_restart_failed") from None
    if result.returncode:
        raise SafeError("credential_service_restart_failed")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if service_ready(service, config):
            return True
        time.sleep(2)
    raise SafeError("credential_service_verification_failed")


def production_identity(helper, config, ui_password=None):
    current = helper.validate(private_json(PRODUCTION_CONFIG))
    if (
        current["instance_uuid"] != config["instance_uuid"]
        or current["team_id"] != "1"
        or current["name"] != "main"
        or current.get("admin_login", "admin") != "admin"
        or current["domain"] != config["production_domain"]
        or current["team_hostname"] != config["oduflow_hostname"]
        or current["ui_password_file"] != str(UI_PASSWORD)
        or current["admin_password_file"] != str(ADMIN_PASSWORD)
    ):
        raise SafeError("credential_production_identity_mismatch")
    receipt = private_json(PRODUCTION_RECEIPT)
    if receipt.get("status") != "hardened" or receipt.get("fingerprint") != helper.fingerprint(
        current
    ):
        raise SafeError("credential_production_receipt_invalid")
    password = private_read(UI_PASSWORD).rstrip("\n") if ui_password is None else ui_password
    authorization = "Basic " + base64.b64encode(("admin:" + password).encode()).decode()
    status, body = request("oduflow", config, "/api/productions/main", authorization)
    try:
        info = json.loads(body)
        if status != 200 or not isinstance(info, dict) or info.get("ok") is not True:
            raise ValueError
    except (ValueError, TypeError):
        raise SafeError("credential_production_unavailable") from None
    helper.verify_info(info, current)
    if info.get("container_status") != "running" or info.get("deploy_in_progress"):
        raise SafeError("credential_production_not_ready")
    container_id = helper.verify_container(info, current, receipt.get("container_id"))
    if not receipt.get("container_id") or container_id != receipt["container_id"]:
        raise SafeError("credential_production_identity_mismatch")
    return info, current, container_id


def rotate_odoo(helper, info, current, container_id, password):
    if helper.verify_container(info, current, container_id) != container_id:
        raise SafeError("credential_production_identity_mismatch")
    launcher = (
        "import os,sys; os.environ['PGPASSWORD']=os.environ['PASSWORD']; "
        "os.execv('/usr/bin/odoo',['odoo','shell','--no-http','--stop-after-init',"
        "'--log-level=critical','--db_host='+os.environ['HOST'],"
        "'--db_user='+os.environ['USER'],'--database='+sys.argv[1]])"
    )
    script = (
        "import json\n"
        "from odoo.exceptions import AccessDenied\n"
        "admin = env.ref('base.user_admin').sudo()\n"
        "assert admin.active and not admin.share and admin.login == 'admin'\n"
        f"desired = {password!r}\n"
        "credential = {'type': 'password', 'login': admin.login, 'password': desired}\n"
        "changed = False\n"
        "try:\n"
        "    auth = admin.with_user(admin)._check_credentials(credential, {'interactive': True})\n"
        "except AccessDenied:\n"
        "    admin.write({'password': desired})\n"
        "    changed = True\n"
        "auth = admin.with_user(admin)._check_credentials(credential, {'interactive': True})\n"
        "assert auth.get('uid') == admin.id and auth.get('auth_method') == 'password'\n"
        "env.cr.commit()\n"
        "print('ODUFLOW_CREDENTIALS ' + json.dumps({'changed': changed, 'verified': True}))\n"
    )
    output = helper.docker_run(
        ["exec", "-i", "--user", "odoo", container_id, "python3", "-c", launcher, info["db_name"]],
        input=script.encode(),
    )
    try:
        lines = [
            line for line in output.decode().splitlines() if line.startswith("ODUFLOW_CREDENTIALS ")
        ]
        if len(lines) != 1:
            raise ValueError
        result = json.loads(lines[0].split(" ", 1)[1])
        if result.get("verified") is not True or type(result.get("changed")) is not bool:
            raise ValueError
        return result["changed"]
    except (ValueError, UnicodeError, TypeError):
        raise SafeError("credential_odoo_verification_failed") from None


def execute(config, helper, storage, state_path, digest_key):
    validate(config)
    verify_storage(config, storage)
    original = private_read(RUNTIME_CONFIG)
    desired_runtime = patch_runtime(original, config)
    desired_paseo = patch_paseo(
        private_read(PASEO_ENV), config["paseo_password"], config["auth_token"]
    )
    desired_digest = hmac.new(
        digest_key, json.dumps(config, sort_keys=True).encode(), hashlib.sha256
    ).hexdigest()
    receipt = private_json(state_path) if state_path.exists() or state_path.is_symlink() else None
    binding = {key: config[key] for key in ("instance_uuid", "request_id", "revision")}
    binding["desired_digest"] = desired_digest
    if receipt:
        revision = receipt.get("revision")
        if (
            type(revision) is not int
            or receipt.get("instance_uuid") != config["instance_uuid"]
            or receipt.get("status") not in ("claimed", "applied")
        ):
            raise SafeError("credential_receipt_mismatch")
        if revision == config["revision"]:
            if any(receipt.get(key) != value for key, value in binding.items()):
                raise SafeError("credential_revision_conflict")
        elif revision + 1 != config["revision"] or receipt.get("status") != "applied":
            raise SafeError("credential_previous_revision_unresolved")
    elif config["revision"] != 1:
        raise SafeError("credential_previous_revision_missing")
    # Verify the current production before writing any service credential.
    # During a retry, its UI password file may already contain desired values
    # while the old Oduflow process still runs; authenticate using the old TOML
    # value only for this fixed local lookup, without replacing its disk copy.
    if receipt and receipt.get("status") == "claimed":
        ensure_service("oduflow", {**config, **tomllib.loads(original)["team"]["1"]})
    info, current, container_id = production_identity(
        helper, config, tomllib.loads(original)["team"]["1"]["ui_password"]
    )
    if (
        receipt
        and receipt["revision"] == config["revision"]
        and receipt.get("container_id") != container_id
    ):
        raise SafeError("credential_production_identity_mismatch")
    save(state_path, {**binding, "status": "claimed", "container_id": container_id})
    changed = atomic_write(RUNTIME_CONFIG, desired_runtime)
    changed = atomic_write(PASEO_ENV, desired_paseo) or changed
    changed = atomic_write(UI_PASSWORD, config["ui_password"] + "\n") or changed
    changed = atomic_write(ADMIN_PASSWORD, config["production_admin_password"] + "\n") or changed
    changed = ensure_service("oduflow", config) or changed
    changed = ensure_service("paseo", config) or changed
    verify_storage(config, storage)
    live_info, live_current, live_container = production_identity(helper, config)
    if live_container != container_id:
        raise SafeError("credential_production_identity_mismatch")
    changed = (
        rotate_odoo(
            helper, live_info, live_current, container_id, config["production_admin_password"]
        )
        or changed
    )
    if not service_ready("oduflow", config) or not service_ready("paseo", config):
        raise SafeError("credential_verification_failed")
    save(state_path, {**binding, "status": "applied", "container_id": container_id})
    return {
        "changed": changed,
        "comment": "client_credentials_verified revision=" + str(config["revision"]),
    }


def main():
    if os.geteuid() != 0:
        raise SafeError("credential_root_required")
    config = validate(private_json(CONFIG))
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = STATE_DIR.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise SafeError("credential_state_directory_untrusted")
    lock = os.open(STATE_DIR / "lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        key_path = STATE_DIR / "hmac.key"
        if not key_path.exists() and not key_path.is_symlink():
            atomic_write(key_path, secrets.token_hex(32))
        key = bytes.fromhex(private_read(key_path))
        if len(key) != 32:
            raise SafeError("credential_receipt_key_invalid")
        helper = trusted_helper("/usr/local/libexec/oduflow-create-production")
        storage = trusted_helper("/usr/local/libexec/oduflow-storage")
        print(json.dumps(execute(config, helper, storage, STATE_DIR / "receipt.json", key)))
    finally:
        os.close(lock)


if __name__ == "__main__":
    try:
        main()
    except SafeError as error:
        print(json.dumps({"changed": False, "comment": str(error)}))
        sys.exit(1)
    except Exception:
        print(json.dumps({"changed": False, "comment": "credential_rotation_failed"}))
        sys.exit(1)
