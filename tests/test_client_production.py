"""Production bootstrap safety; local files and mocked HTTP/Docker only."""

import importlib.util
import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "client_production", ROOT / "salt/states/client_production/files/create-production.py"
)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


class ProductionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.receipt = self.directory / "receipt.json"
        self.password = "unique-strong-password-for-demo-'secret'"
        self.password_file = self.directory / "password"
        self.password_file.write_text(self.password)
        self.config = {
            "instance_uuid": "00000000-0000-4000-8000-000000000001",
            "name": "main",
            "domain": "client.example.org",
            "repo_url": "https://github.com/oduflow/client.git",
            "branch": "main",
            "team_id": "1",
            "team_hostname": "oduflow.client.example.org",
            "ui_password_file": str(self.password_file),
            "admin_password_file": str(self.password_file),
        }
        self.info = helper.expected(self.config) | {
            "odoo_container": "oduflow-1-prod-main-odoo",
            "db_name": "oduflow_1_prod_main",
            "container_status": "running",
            "deploy_in_progress": False,
        }
        self.api = Mock()
        self.harden = Mock(return_value="a" * 64)
        self.verify_container = Mock(return_value="a" * 64)
        self.guard = Mock()
        patches = [
            patch.object(helper, "private_read", lambda p: Path(p).read_text()),
            patch.object(helper, "harden", self.harden),
            patch.object(helper, "verify_container", self.verify_container),
            patch.object(helper, "verify_guard", self.guard),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def run_helper(self):
        return helper.execute(self.config, self.api, self.receipt)

    def claim(self):
        helper.save_state(
            self.receipt, {"fingerprint": helper.fingerprint(self.config), "status": "claimed"}
        )

    def test_first_create_claimed_before_post_then_password_before_ready(self):
        self.api.info.side_effect = [None, self.info]

        def create():
            self.assertEqual(json.loads(self.receipt.read_text())["status"], "claimed")
            self.harden.assert_not_called()

        self.api.create.side_effect = create
        self.assertEqual(self.run_helper()["status"], "hardened")
        self.api.create.assert_called_once()
        self.harden.assert_called_once_with(self.info, self.config, self.password, None)
        state = json.loads(self.receipt.read_text())
        self.assertEqual(state["status"], "hardened")
        self.assertNotIn(self.password, self.receipt.read_text())
        self.assertEqual(stat.S_IMODE(self.receipt.stat().st_mode), 0o600)

    def test_uncertain_create_can_reconcile_matching_resource_without_second_post(self):
        self.api.info.return_value = None
        self.api.create.side_effect = helper.SafeError("local_api_outcome_unknown")
        with self.assertRaisesRegex(helper.SafeError, "outcome_unknown"):
            self.run_helper()
        self.api.info.return_value = self.info
        self.assertEqual(self.run_helper()["status"], "hardened")
        self.assertEqual(self.api.create.call_count, 1)

    def test_uncertain_create_without_resource_never_reposts(self):
        self.claim()
        self.api.info.return_value = None
        with self.assertRaisesRegex(helper.SafeError, "outcome_unknown"):
            self.run_helper()
        self.api.create.assert_not_called()
        self.harden.assert_not_called()

    def test_existing_without_claim_is_never_modified(self):
        self.api.info.return_value = self.info
        with self.assertRaisesRegex(helper.SafeError, "not_dispatched"):
            self.run_helper()
        self.api.create.assert_not_called()
        self.harden.assert_not_called()

    def test_mismatched_existing_metadata_is_never_modified(self):
        self.claim()
        for key in ("name", "domain", "repo_url", "branch", "odoo_image"):
            self.api.info.return_value = self.info | {key: "foreign"}
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(helper.SafeError, "metadata_mismatch"),
            ):
                self.run_helper()
        self.harden.assert_not_called()

    def test_completed_rerun_does_not_reset_password_or_require_closed_firewall(self):
        helper.save_state(
            self.receipt,
            {
                "fingerprint": helper.fingerprint(self.config),
                "status": "hardened",
                "container_id": "a" * 64,
            },
        )
        self.api.info.return_value = self.info
        self.assertFalse(self.run_helper()["changed"])
        self.guard.assert_not_called()
        self.harden.assert_not_called()
        self.api.create.assert_not_called()
        self.verify_container.assert_called_once_with(self.info, self.config, "a" * 64)

    def test_completed_receipt_cannot_certify_replacement_container(self):
        helper.save_state(
            self.receipt,
            {
                "fingerprint": helper.fingerprint(self.config),
                "status": "hardened",
                "container_id": "a" * 64,
            },
        )
        self.api.info.return_value = self.info
        self.verify_container.side_effect = helper.SafeError("docker_identity_mismatch")
        with self.assertRaisesRegex(helper.SafeError, "docker_identity_mismatch"):
            self.run_helper()
        self.harden.assert_not_called()
        self.api.create.assert_not_called()

    def test_completed_receipt_without_container_identity_fails_closed(self):
        helper.save_state(
            self.receipt, {"fingerprint": helper.fingerprint(self.config), "status": "hardened"}
        )
        self.api.info.return_value = self.info
        with self.assertRaisesRegex(helper.SafeError, "receipt_missing_container"):
            self.run_helper()
        self.harden.assert_not_called()
        self.api.create.assert_not_called()

    def test_guard_failure_prevents_claim_and_all_mutations(self):
        self.api.info.return_value = None
        self.guard.side_effect = helper.SafeError("publication_guard_missing_or_mismatched")
        with self.assertRaisesRegex(helper.SafeError, "publication_guard"):
            self.run_helper()
        self.assertFalse(self.receipt.exists())
        self.api.create.assert_not_called()
        self.harden.assert_not_called()

    def test_failed_password_commit_keeps_claim_for_safe_resume(self):
        self.claim()
        self.api.info.return_value = self.info
        self.harden.side_effect = helper.SafeError("admin_password_commit_unconfirmed")
        with self.assertRaisesRegex(helper.SafeError, "commit_unconfirmed"):
            self.run_helper()
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "claimed")
        self.api.create.assert_not_called()

    def test_invalid_config_including_secret_url_is_rejected(self):
        for change in (
            {"repo_url": "https://user:SECRET@github.com/oduflow/client.git"},
            {"repo_url": "git@github.com:oduflow/client.git"},
            {"name": "../foreign"},
            {"api_port": True},
            {"admin_password_file": "relative"},
        ):
            with self.subTest(change=change), self.assertRaises(helper.SafeError):
                helper.validate(self.config | change)


class SecretAndDockerTests(unittest.TestCase):
    def test_git_token_only_in_credential_stdin_and_clean_branch_probe(self):
        token = "github-fixture-secret-token"
        config = {
            "team_id": "1",
            "git_username": "x-access-token",
            "git_token_file": "/secret",
            "repo_url": "https://github.com/oduflow/client.git",
            "branch": "main",
        }
        info = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0)
        with (
            patch.object(helper, "private_read", return_value=token),
            patch.object(Path, "mkdir"),
            patch.object(Path, "lstat", return_value=info),
            patch.object(Path, "exists", return_value=False),
            patch.object(Path, "is_symlink", return_value=False),
            patch.object(Path, "chmod"),
            patch.object(helper.subprocess, "run", return_value=Mock(returncode=0)) as command,
        ):
            helper.setup_git(config)
        first, second = command.call_args_list
        self.assertEqual(first.args[0], ["git", "credential", "approve"])
        self.assertIn(token.encode(), first.kwargs["input"])
        for call in command.call_args_list:
            self.assertNotIn(token, repr(call.args))
            self.assertNotIn(token, repr(call.kwargs["env"]))
        self.assertEqual(
            second.args[0],
            ["git", "ls-remote", "--exit-code", "--heads", config["repo_url"], "main"],
        )

    def test_runtime_team_hostname_password_and_api_binding_must_match(self):
        config = {
            "team_id": "1",
            "team_hostname": "oduflow.client.example.org",
            "api_host": "172.17.0.1",
            "ui_password_file": "/secret",
        }
        toml = """[server]
bind = "172.17.0.1"
port = 8000
[storage]
data_dir = "/srv/oduflow/data"
[team.1]
hostname = "oduflow.client.example.org"
ui_password = "fixture-password"
"""
        with patch.object(
            helper,
            "private_read",
            side_effect=lambda p: toml if p == helper.ODUFLOW_CONFIG else "fixture-password",
        ):
            helper.verify_runtime_config(config)
            for change in (
                {"team_hostname": "another.example.org"},
                {"api_host": "127.0.0.1"},
                {"team_id": "other"},
            ):
                with self.subTest(change=change), self.assertRaises(helper.SafeError):
                    helper.verify_runtime_config(config | change)

    def test_private_files_reject_symlinks_and_wrong_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret"
            path.write_text("secret")
            symlink = Path(tmp) / "link"
            symlink.symlink_to(path)
            with self.assertRaisesRegex(helper.SafeError, "private_file_unavailable"):
                helper.private_read(symlink)
            metadata = SimpleNamespace(
                st_mode=stat.S_IFREG | 0o644, st_uid=0, st_nlink=1, st_size=6
            )
            with (
                patch.object(os, "fstat", return_value=metadata),
                self.assertRaisesRegex(helper.SafeError, "permissions"),
            ):
                helper.private_read(path)
            metadata.st_mode = stat.S_IFREG | 0o600
            with patch.object(os, "fstat", return_value=metadata):
                self.assertEqual(helper.private_read(path), "secret")

    def test_api_creation_contract_is_clean_and_template_free(self):
        config = {
            "ui_password_file": "/secret",
            "team_hostname": "oduflow.client.example.org",
            "name": "main",
            "domain": "client.example.org",
            "repo_url": "https://github.com/oduflow/client.git",
            "branch": "main",
        }
        with patch.object(helper, "private_read", return_value="api-password"):
            api = helper.LocalAPI(config)
        with patch.object(api, "request", return_value={"ok": True}) as request:
            api.create()
        payload = request.call_args.args[2]
        self.assertEqual(payload["template_name"], "")
        self.assertEqual(payload["odoo_image"], "odoo:19.0")
        self.assertNotIn("api-password", json.dumps(payload))

    def test_api_rejects_redirect_without_following_or_exposing_response(self):
        config = {"ui_password_file": "/secret", "team_hostname": "oduflow.client.example.org"}
        response = Mock(status=302)
        with (
            patch.object(helper, "private_read", return_value="api-password"),
            patch.object(helper.http.client, "HTTPConnection") as factory,
        ):
            factory.return_value.getresponse.return_value = response
            api = helper.LocalAPI(config)
            with self.assertRaisesRegex(helper.SafeError, "local_api_http_302"):
                api.request("POST", "/api/productions/create", {})
            factory.assert_called_once_with("127.0.0.1", 8000, timeout=900)
            factory.return_value.request.assert_called_once()
            response.read.assert_not_called()

    def test_password_only_in_stdin_and_exec_targets_inspected_id(self):
        config = {
            "team_id": "1",
            "name": "main",
            "domain": "client.example.org",
            "repo_url": "https://github.com/oduflow/client.git",
            "branch": "main",
        }
        labels = {
            "oduflow.managed": "true",
            "oduflow.prod": "true",
            "oduflow.team": "1",
            "oduflow.prod_name": "main",
            "oduflow.domain": config["domain"],
            "oduflow.repo": config["repo_url"],
            "oduflow.git_branch": "main",
            "oduflow.image": "odoo:19.0",
        }
        container = {"Id": "a" * 64, "Config": {"Labels": labels}}
        info = {"odoo_container": "production-name", "db_name": "production_db"}
        password = "VERY-SECRET-'\n-password"
        with patch.object(
            helper,
            "docker_run",
            side_effect=[json.dumps([container]).encode(), b"ODUFLOW_ADMIN_HARDENED\n"],
        ) as command:
            self.assertEqual(helper.harden(info, config, password), "a" * 64)
            args = command.call_args.args[0]
            self.assertEqual(args[4], "a" * 64)
            self.assertNotIn(password, repr(args))
            script = command.call_args.kwargs["input"].decode()
            compile(script, "stdin", "exec")
            self.assertIn(repr(password), script)
            self.assertIn("env.cr.commit()", script)
        self.assertNotIn("--db_password", repr(args))


class ProductionSaltTests(unittest.TestCase):
    def pillar(self):
        return {
            "schema": 1,
            "instance_uuid": "00000000-0000-4000-8000-000000000001",
            "storage": {"device": "/dev/disk/by-id/fixture", "mount": "/srv/oduflow/data"},
            "dns": {
                "production": "client.example.org",
                "oduflow": "oduflow.client.example.org",
                "paseo": "paseo.client.example.org",
            },
            "ingress": {"mode": "direct_tls", "acme": {"email": "admin@example.org"}},
            "oduflow": {
                "auth_token": "fixture-auth-token-long",
                "database_password": "fixture-database-password-long",
                "ui_password": "fixture-ui-password-long",
                "production_admin_password": "fixture-admin-password-very-long",
                "git": {
                    "host": "github.com",
                    "username": "x-access-token",
                    "token": "fixture-github-token-long",
                    "repo": "oduflow/client",
                    "branch": "main",
                },
            },
            "paseo": {"password": "fixture-paseo-password-long"},
        }

    def render(self, pillar, template="client_production/init.sls"):
        def get(path, default=None):
            value = pillar
            for part in path.split(":"):
                if not isinstance(value, dict) or part not in value:
                    return default
                value = value[part]
            return value

        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(ROOT / "salt/states")),
            undefined=jinja2.StrictUndefined,
        )
        env.filters["regex_match"] = lambda value, pattern: re.match(pattern, value)
        text = env.get_template(template).render(
            salt={"pillar.get": get}, grains={"id": "client-" + pillar["instance_uuid"]}
        )
        return yaml.safe_load(text)

    @staticmethod
    def args(state):
        return {key: value for item in next(iter(state.values())) for key, value in item.items()}

    def test_secret_files_are_private_config_url_clean_and_command_guarded(self):
        data = self.render(self.pillar())
        for key in ("ui-password", "admin-password", "git-token"):
            state = self.args(data["client-production-" + key])
            self.assertEqual(state["mode"], "0600")
            self.assertFalse(state["show_changes"])
            self.assertIn("contents_pillar", state)
        config = json.loads(self.args(data["client-production-config"])["contents"])
        self.assertEqual(config["repo_url"], "https://github.com/oduflow/client.git")
        self.assertEqual(config["api_host"], "172.17.0.1")
        self.assertNotIn("fixture-github-token", json.dumps(config))
        command = self.args(data["client-production-create"])
        self.assertEqual(
            command["onlyif"], "test -f /etc/oduflow/production-publication-guard.json"
        )
        self.assertEqual(command["name"], "/usr/local/libexec/oduflow-create-production")

    def test_missing_branch_or_password_refuses_runtime_state(self):
        for field in ("branch", "production_admin_password"):
            pillar = self.pillar()
            if field == "branch":
                pillar["oduflow"]["git"].pop(field)
            else:
                pillar["oduflow"].pop(field)
            data = self.render(pillar)
            self.assertEqual(list(data), ["client-production-invalid-pillar"])

    def test_automatic_publication_has_health_and_secret_dependencies(self):
        pillar = self.pillar()
        pillar["network"] = {"public_ipv4": "136.244.104.213"}
        data = self.render(pillar, "client_production/publish.sls")
        self.assertEqual(data["include"], ["client_production.configure", "client_apps.health"])
        self.assertNotIn("client-production-create", data)
        command = self.args(data["client-production-publish"])
        self.assertNotIn("onlyif", command)
        self.assertIn({"cmd": "client-apps-health"}, command["require"])
        self.assertIn({"file": "client-production-admin-password"}, command["require"])
        self.assertIn({"pkg": "client-production-verifier-dependency"}, command["require"])
        unit = self.args(data["client-production-boot-guard-unit"])["contents"]
        self.assertIn("Before=docker.service", unit)
        self.assertIn("--boot-guard", unit)
        dropin = self.args(data["client-production-docker-boot-guard"])["contents"]
        self.assertIn("Requires=oduflow-production-ingress-guard.service", dropin)

    def test_automatic_publication_requires_verified_network_pillar(self):
        data = self.render(self.pillar(), "client_production/publish.sls")
        self.assertEqual(list(data), ["client-production-publication-invalid"])


if __name__ == "__main__":
    unittest.main()
