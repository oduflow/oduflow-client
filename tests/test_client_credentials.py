"""Credential rotation contracts with local fixtures and no real service mutation."""

import copy
import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import tomllib
from test_client_apps import PILLAR, args, high

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "client_credentials", ROOT / "salt/states/client_credentials/files/rotate.py"
)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)
UUID = "12345678-1234-1234-1234-123456789abc"
REQUEST = "12345678-1234-4234-9234-123456789abc"
RUNTIME = """[server]
host = "172.17.0.1"
port = 8000
[storage]
data_dir = "/srv/oduflow/data"
[database]
user = "odoo"
password = "database-password-must-never-change"
[team.1]
hostname = "oduflow.acme.example.com"
auth_token = "old-mcp-token-fixture-abcdefghijkl"
ui_password = "old-ui-password-fixture-abcdefghijkl"
environment_slots = 5
[route.paseo]
host = "paseo.acme.example.com"
url = "http://172.17.0.1:6768"
"""


def desired():
    return {
        "instance_uuid": UUID,
        "request_id": REQUEST,
        "revision": 1,
        "device": "/dev/disk/by-id/test",
        "oduflow_hostname": "oduflow.acme.example.com",
        "paseo_hostname": "paseo.acme.example.com",
        "production_domain": "acme.example.com",
        "ui_password": "new-ui-password-fixture-abcdefghijkl",
        "auth_token": "new-mcp-token-fixture-abcdefghijkl",
        "paseo_password": "new-paseo-password-fixture-abcdefghijkl",
        "production_admin_password": "new-admin-password-fixture-abcdefghijkl",
    }


class RotationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config = desired()
        self.receipt = self.directory / "receipt.json"
        self.production = Mock()
        self.storage = Mock()
        self.identity = Mock(return_value=({"db_name": "prod_db"}, {}, "a" * 64))
        self.service = Mock(return_value=False)
        self.odoo = Mock(return_value=False)
        for name, content in (
            ("RUNTIME_CONFIG", RUNTIME),
            ("PASEO_ENV", "PASEO_PASSWORD=old-password-abcdefghijkl\n"),
            ("UI_PASSWORD", "old-ui-password-fixture-abcdefghijkl\n"),
            ("ADMIN_PASSWORD", "old-admin-password-fixture-abcdefghijkl\n"),
        ):
            path = self.directory / name
            path.write_text(content)
            item = patch.object(helper, name, path)
            item.start()
            self.addCleanup(item.stop)
        for item in (
            patch.object(helper, "private_read", lambda path: Path(path).read_text()),
            patch.object(helper, "verify_storage"),
            patch.object(helper, "production_identity", self.identity),
            patch.object(helper, "ensure_service", self.service),
            patch.object(helper, "rotate_odoo", self.odoo),
            patch.object(helper, "service_ready", return_value=True),
        ):
            item.start()
            self.addCleanup(item.stop)

    def execute(self):
        return helper.execute(self.config, self.production, self.storage, self.receipt, b"x" * 32)

    def test_rotation_updates_mcp_token_and_preserves_unrelated_environment(self):
        helper.PASEO_ENV.write_text(
            "PASEO_PASSWORD=old-password-abcdefghijkl\n"
            "ODUFLOW_MCP_TOKEN=old-mcp-token-fixture-abcdefghijkl\n"
            "UNRELATED=preserved\n"
        )
        self.execute()
        contents = helper.PASEO_ENV.read_text()
        self.assertIn("ODUFLOW_MCP_TOKEN=" + self.config["auth_token"] + "\n", contents)
        self.assertIn("UNRELATED=preserved\n", contents)
        self.assertNotIn("old-mcp-token", contents)

    def test_first_rotation_preserves_database_and_records_no_secrets(self):
        result = self.execute()
        self.assertTrue(result["changed"])
        runtime = tomllib.loads(helper.RUNTIME_CONFIG.read_text())
        original = tomllib.loads(RUNTIME)
        self.assertEqual(runtime["database"], original["database"])
        self.assertEqual(runtime["team"]["1"]["ui_password"], self.config["ui_password"])
        self.assertEqual(runtime["team"]["1"]["auth_token"], self.config["auth_token"])
        self.assertEqual(runtime["route"], original["route"])
        self.assertEqual(
            helper.ADMIN_PASSWORD.read_text().strip(), self.config["production_admin_password"]
        )
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "applied")
        self.assertEqual(stat.S_IMODE(self.receipt.stat().st_mode), 0o600)
        for secret in helper.SECRET_NAMES:
            self.assertNotIn(self.config[secret], self.receipt.read_text())
            self.assertNotIn(self.config[secret], json.dumps(result))
        self.assertEqual(self.odoo.call_args.args[3], "a" * 64)

    def test_same_revision_is_idempotent_without_rewriting_credentials(self):
        self.execute()
        before = helper.RUNTIME_CONFIG.stat().st_mtime_ns
        self.assertFalse(self.execute()["changed"])
        self.assertEqual(helper.RUNTIME_CONFIG.stat().st_mtime_ns, before)
        self.assertEqual(json.loads(self.receipt.read_text())["revision"], 1)

    def test_revision_conflict_stale_and_skipped_revisions_are_refused(self):
        self.execute()
        for changes in (
            {"ui_password": "different-new-password-fixture-abcdefgh"},
            {"request_id": "22345678-1234-4234-9234-123456789abc"},
            {"revision": 0},
            {"revision": 3},
        ):
            with self.subTest(changes=changes), self.assertRaises(helper.SafeError):
                self.config = {**desired(), **changes}
                self.execute()
        self.config = desired() | {
            "revision": 2,
            "request_id": "22345678-1234-4234-9234-123456789abc",
        }
        self.execute()
        self.assertEqual(json.loads(self.receipt.read_text())["revision"], 2)
        self.config = desired()
        with self.assertRaises(helper.SafeError):
            self.execute()

    def test_partial_failure_reuses_desired_revision_and_converges(self):
        self.service.side_effect = helper.SafeError("credential_service_restart_failed")
        with self.assertRaises(helper.SafeError):
            self.execute()
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt["status"], "claimed")
        self.assertEqual(receipt["request_id"], REQUEST)
        self.odoo.assert_not_called()
        self.service.side_effect = None
        self.execute()
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "applied")
        self.assertEqual(json.loads(self.receipt.read_text())["request_id"], REQUEST)
        self.assertEqual(self.identity.call_args_list[-2].args[2], self.config["ui_password"])

    def test_partial_revision_cannot_be_bypassed(self):
        self.service.side_effect = helper.SafeError("credential_service_restart_failed")
        with self.assertRaises(helper.SafeError):
            self.execute()
        self.config.update(revision=2, request_id="22345678-1234-4234-9234-123456789abc")
        self.service.side_effect = None
        with self.assertRaisesRegex(helper.SafeError, "previous_revision_unresolved"):
            self.execute()

    def test_changed_container_or_failed_ownership_prevents_password_write(self):
        self.identity.side_effect = helper.SafeError("credential_production_identity_mismatch")
        with self.assertRaises(helper.SafeError):
            self.execute()
        self.assertEqual(helper.RUNTIME_CONFIG.read_text(), RUNTIME)
        self.assertFalse(self.receipt.exists())
        self.odoo.assert_not_called()
        self.identity.side_effect = None
        self.execute()
        self.odoo.reset_mock()
        self.identity.return_value = ({"db_name": "prod_db"}, {}, "b" * 64)
        with self.assertRaisesRegex(helper.SafeError, "production_identity_mismatch"):
            self.execute()
        self.odoo.assert_not_called()


class HelperContracts(unittest.TestCase):
    def test_rejects_invalid_revision_secret_and_wrong_runtime_identity(self):
        for changes in (
            {"revision": True},
            {"request_id": "invalid"},
            {"paseo_password": "short"},
            {"ui_password": "password-with-newline-abcdefgh\n"},
        ):
            with self.assertRaises(helper.SafeError):
                helper.validate(desired() | changes)
        with self.assertRaises(helper.SafeError):
            helper.patch_runtime(RUNTIME.replace("oduflow.acme", "oduflow.foreign"), desired())
        with self.assertRaises(helper.SafeError):
            helper.patch_runtime(RUNTIME + '\n[team.2]\nhostname="other.example.com"\n', desired())

    def test_storage_verification_is_readonly_and_binds_filesystem(self):
        storage = Mock()
        with patch.object(
            helper,
            "private_json",
            return_value={
                "instance_uuid": UUID,
                "device": "/dev/disk/by-id/test",
                "filesystem_uuid": "xfs-fixture",
            },
        ):
            helper.verify_storage(desired(), storage)
        argument = storage.prepare.call_args.args[0]
        self.assertTrue(argument.verify)
        self.assertFalse(argument.allow_format)
        self.assertEqual(argument.filesystem_uuid, "xfs-fixture")
        with patch.object(helper, "private_json", return_value={"instance_uuid": "foreign"}):
            with self.assertRaisesRegex(helper.SafeError, "storage_identity_mismatch"):
                helper.verify_storage(desired(), storage)

    def test_private_files_refuse_symlinks_nonroot_and_world_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private"
            path.write_text("secret-fixture")
            metadata = SimpleNamespace(
                st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1, st_size=14
            )
            with patch.object(os, "fstat", return_value=metadata):
                self.assertEqual(helper.private_read(path), "secret-fixture")
            for field, value in (
                ("st_uid", 1000),
                ("st_mode", stat.S_IFREG | 0o644),
                ("st_nlink", 2),
            ):
                changed = copy.copy(metadata)
                setattr(changed, field, value)
                with (
                    patch.object(os, "fstat", return_value=changed),
                    self.assertRaises(helper.SafeError),
                ):
                    helper.private_read(path)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(helper.SafeError):
                helper.private_read(link)

    def test_verified_services_do_not_restart_and_only_fixed_services_can_restart(self):
        with (
            patch.object(helper, "service_ready", return_value=True),
            patch.object(subprocess := helper.subprocess, "run") as run,
        ):
            self.assertFalse(helper.ensure_service("oduflow", desired()))
            self.assertFalse(helper.ensure_service("paseo", desired()))
            run.assert_not_called()
        with (
            patch.object(helper, "service_ready", return_value=False),
            patch.object(subprocess, "run") as run,
        ):
            with self.assertRaises(helper.SafeError):
                helper.ensure_service("docker", desired())
            run.assert_not_called()

    def test_mcp_probe_uses_bearer_headers_and_requires_successful_initialize(self):
        responses = [
            (401, b""),
            (200, b"{}"),
            (401, b""),
            (200, b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26"}}'),
        ]
        with patch.object(helper, "request", side_effect=responses) as request:
            self.assertTrue(helper.service_ready("oduflow", desired()))
        call = request.call_args_list[-1]
        self.assertEqual(call.args[2], "/mcp")
        self.assertEqual(call.args[3], "Bearer " + desired()["auth_token"])
        self.assertEqual(call.args[4]["method"], "initialize")
        responses[-1] = (200, b'{"error":{"message":"secret-fixture"}}')
        with patch.object(helper, "request", side_effect=responses):
            self.assertFalse(helper.service_ready("oduflow", desired()))

    def test_production_lookup_uses_current_runtime_password_after_partial_write(self):
        production = Mock()
        config = desired()
        current = {
            "instance_uuid": UUID,
            "team_id": "1",
            "name": "main",
            "domain": config["production_domain"],
            "team_hostname": config["oduflow_hostname"],
            "ui_password_file": str(helper.UI_PASSWORD),
            "admin_password_file": str(helper.ADMIN_PASSWORD),
        }
        production.validate.return_value = current
        production.fingerprint.return_value = "production-fingerprint"
        production.verify_container.return_value = "a" * 64
        info = {"ok": True, "container_status": "running", "deploy_in_progress": False}
        with (
            patch.object(
                helper,
                "private_json",
                side_effect=[
                    current,
                    {
                        "status": "hardened",
                        "fingerprint": "production-fingerprint",
                        "container_id": "a" * 64,
                    },
                ],
            ),
            patch.object(helper, "private_read") as read,
            patch.object(
                helper, "request", return_value=(200, json.dumps(info).encode())
            ) as request,
        ):
            result = helper.production_identity(production, config, "current-runtime-password")
        read.assert_not_called()
        self.assertEqual(result[2], "a" * 64)
        self.assertEqual(request.call_args.args[:3], ("oduflow", config, "/api/productions/main"))
        self.assertEqual(
            request.call_args.args[3], "Basic YWRtaW46Y3VycmVudC1ydW50aW1lLXBhc3N3b3Jk"
        )
        production.LocalAPI.assert_not_called()

    def test_production_password_stays_on_stdin_and_execution_uses_container_id(self):
        production = Mock()
        production.verify_container.return_value = "a" * 64
        production.docker_run.return_value = (
            b'ODUFLOW_CREDENTIALS {"changed": true, "verified": true}\n'
        )
        result = helper.rotate_odoo(
            production, {"db_name": "prod_db"}, {}, "a" * 64, desired()["production_admin_password"]
        )
        self.assertTrue(result)
        arguments = production.docker_run.call_args.args[0]
        self.assertEqual(arguments[:5], ["exec", "-i", "--user", "odoo", "a" * 64])
        self.assertNotIn(desired()["production_admin_password"], repr(arguments))
        script = production.docker_run.call_args.kwargs["input"].decode()
        self.assertIn("_check_credentials", script)
        self.assertIn("env.cr.commit()", script)
        self.assertNotIn("restart", script)
        self.assertNotIn("ALTER ROLE", script)


class StateContracts(unittest.TestCase):
    def test_invalid_pillar_never_installs_or_runs_rotation(self):
        self.assertEqual(list(high("client_credentials/init.sls")), ["client-credentials-invalid"])
        pillar = copy.deepcopy(PILLAR)
        pillar["credentials"] = {"request_id": REQUEST, "revision": True}
        self.assertEqual(
            list(high("client_credentials/init.sls", pillar)), ["client-credentials-invalid"]
        )

    def test_fixed_helper_private_desired_file_and_no_full_application_apply(self):
        pillar = copy.deepcopy(PILLAR)
        pillar["credentials"] = {"request_id": REQUEST, "revision": 1}
        config = desired()
        pillar["oduflow"].update(
            {key: config[key] for key in ("ui_password", "auth_token", "production_admin_password")}
        )
        pillar["paseo"]["password"] = config["paseo_password"]
        data = high("client_credentials/init.sls", pillar)
        self.assertNotIn("include", data)
        private = args(data["client-credentials-config"])
        self.assertEqual(private["mode"], "0600")
        self.assertFalse(private["show_changes"])
        run = args(data["client-credentials-apply"])
        self.assertEqual(run["name"], "/usr/local/libexec/oduflow-rotate-credentials")
        self.assertTrue(run["stateful"])
        for secret in helper.SECRET_NAMES:
            self.assertNotIn(config[secret], run["name"])
