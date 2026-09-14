import importlib.util
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "client_backup", ROOT / "salt/states/client_backup/files/backup.py"
)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)

UUID = "11111111-2222-4333-8444-555555555555"


class ClientBackupTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source = root / "data"
        self.source.mkdir()
        self.receipt = root / "storage.json"
        self.receipt.write_text(json.dumps({"instance_uuid": UUID}))
        self.probe = self.source / ".oduflow-backup/restore-probe.json"
        patches = (
            patch.object(backup, "SOURCE", self.source),
            patch.object(backup, "STORAGE_RECEIPT", self.receipt),
            patch.object(backup, "PROBE", self.probe),
            patch.object(backup.os.path, "ismount", return_value=True),
            patch.dict(
                os.environ,
                {
                    "RESTIC_REPOSITORY": "s3:https://account.eu.r2.example/bucket",
                    "RESTIC_PASSWORD": "p" * 32,
                    "AWS_ACCESS_KEY_ID": "a" * 32,
                    "AWS_SECRET_ACCESS_KEY": "b" * 64,
                    "ODUFLOW_INSTANCE_UUID": UUID,
                    "ODUFLOW_BACKUP_RETENTION_DAYS": "30",
                },
            ),
        )
        for current in patches:
            current.start()
            self.addCleanup(current.stop)

    def test_environment_binds_backup_to_owned_data_volume(self):
        self.assertEqual(backup.validate_environment(), (UUID, 30))
        self.receipt.write_text(json.dumps({"instance_uuid": str(UUID).replace("1", "2")}))
        with self.assertRaisesRegex(backup.BackupError, "ownership"):
            backup.validate_environment()

    def test_services_restart_after_backup_failure(self):
        with (
            patch.object(backup, "ensure_probe", return_value=b"probe") as probe,
            patch.object(backup, "ensure_repository", return_value=False),
            patch.object(backup, "active_units", return_value=["docker.service"]),
            patch.object(backup, "stop_units") as stop,
            patch.object(backup, "restart_units") as restart,
            patch.object(backup, "run", side_effect=RuntimeError("failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                backup.backup(UUID, 30, True)
        probe.assert_called_once_with(UUID)
        stop.assert_called_once_with(["docker.service"])
        restart.assert_called_once_with(["docker.service"])

    def test_cold_copy_disables_docker_socket_activation_and_restores_units(self):
        active = set(backup.UNITS)

        def command(*args, **kwargs):
            if args[:2] == ("systemctl", "is-active"):
                return Mock(returncode=0 if args[-1] in active else 3)
            if args[:2] == ("systemctl", "stop"):
                active.discard(args[2])
            elif args[:2] == ("systemctl", "start"):
                active.add(args[2])
            elif args[:2] == ("restic", "backup"):
                self.assertNotIn("docker.service", active)
                self.assertNotIn("docker.socket", active)
                self.assertNotIn("containerd.service", active)
            return Mock(returncode=0)

        with (
            patch.object(backup, "ensure_probe", return_value=b"probe"),
            patch.object(backup, "ensure_repository", return_value=False),
            patch.object(backup, "run", side_effect=command),
        ):
            backup.backup(UUID, 30, False)
        self.assertEqual(active, set(backup.UNITS))

    def test_restart_failure_does_not_skip_other_previously_active_services(self):
        calls = []

        def command(*args, **kwargs):
            calls.append(args[-1])
            if args[-1] == "docker.service":
                raise backup.BackupError("client_service_restart_failed")

        with patch.object(backup, "run", side_effect=command):
            with self.assertRaises(backup.BackupError):
                backup.restart_units(list(backup.UNITS))
        self.assertEqual(calls, list(backup.RESTART_ORDER))

    def test_restore_reads_the_exact_probe_from_latest_snapshot(self):
        expected = b"restore-proof\n"

        def command(*args, **kwargs):
            result = Mock(returncode=0, stdout="")
            if args[:2] == ("restic", "snapshots"):
                result.stdout = json.dumps([{"id": "a" * 64}])
            elif args[:2] == ("restic", "restore"):
                target = Path(args[args.index("--target") + 1])
                restored = target / str(self.probe).removeprefix("/")
                restored.parent.mkdir(parents=True)
                restored.write_bytes(expected)
            return result

        with patch.object(backup, "run", side_effect=command):
            digest = backup.verify_restore(UUID, expected)
        self.assertEqual(digest, __import__("hashlib").sha256(expected).hexdigest())

    def test_state_installs_timer_only_after_restore_test(self):
        state = (ROOT / "salt/states/client_backup/init.sls").read_text()
        role = (ROOT / "salt/states/roles/client_production.sls").read_text()
        self.assertIn(". /etc/oduflow/backup.env", state)
        self.assertIn("exec /usr/local/libexec/oduflow-backup --verify-restore", state)
        self.assertIn("- cmd: client-backup-initial-restore-test", state)
        self.assertIn("show_changes: false", state)
        self.assertIn("pillar.get']('backup:enabled', False)", role)

    def render_state(self, path, pillar):
        def get(key, default=None):
            value = pillar
            for part in key.split(":"):
                if not isinstance(value, dict) or part not in value:
                    return default
                value = value[part]
            return value

        environment = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(ROOT / "salt/states")),
            undefined=jinja2.StrictUndefined,
        )
        environment.filters["regex_match"] = lambda value, pattern: re.match(pattern, value)
        rendered = environment.get_template(path).render(
            salt={"pillar.get": get}, grains={"id": "client-" + UUID}
        )
        return yaml.safe_load(rendered)

    def backup_pillar(self):
        return {
            "schema": 1,
            "instance_uuid": UUID,
            "backup": {
                "enabled": True,
                "provider": "cloudflare_r2",
                "bucket": "oduflow-" + UUID,
                "jurisdiction": "eu",
                "endpoint": "https://" + "a" * 32 + ".eu.r2.cloudflarestorage.com",
                "access_key_id": "b" * 32,
                "secret_access_key": "c" * 64,
                "repository_password": "fixture-repository-password-long",
                "retention_days": 30,
            },
        }

    def test_valid_state_is_bound_to_the_exact_minion_and_hides_changes(self):
        data = self.render_state("client_backup/init.sls", self.backup_pillar())
        self.assertNotIn("client-backup-invalid-pillar", data)
        environment = data["client-backup-environment"]["file.managed"]
        arguments = {key: value for item in environment for key, value in item.items()}
        self.assertEqual(arguments["mode"], "0600")
        self.assertFalse(arguments["show_changes"])
        self.assertIn("RESTIC_CACHE_DIR=/srv/oduflow/data/", arguments["contents"])

    def test_mismatched_identity_or_unscoped_secret_refuses_backup(self):
        for field, value in (
            ("instance_uuid", "21111111-2222-4333-8444-555555555555"),
            ("access_key_id", "not-scoped"),
            ("retention_days", True),
        ):
            pillar = self.backup_pillar()
            if field == "instance_uuid":
                pillar[field] = value
            else:
                pillar["backup"][field] = value
            with self.subTest(field=field):
                data = self.render_state("client_backup/init.sls", pillar)
                self.assertEqual(list(data), ["client-backup-invalid-pillar"])

    def test_production_role_includes_backup_only_when_enabled(self):
        disabled = self.render_state("roles/client_production.sls", {})
        enabled = self.render_state("roles/client_production.sls", self.backup_pillar())
        self.assertEqual(disabled["include"], ["client_production.publish"])
        self.assertEqual(enabled["include"], ["client_production.publish", "client_backup"])


if __name__ == "__main__":
    unittest.main()
