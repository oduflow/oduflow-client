"""Render the complete customer configuration without network or cloud mutations."""

import copy
import unittest

import tomllib
from test_client_apps import PILLAR, high, render


class CustomerSettingsRendering(unittest.TestCase):
    def test_full_settings_and_escaping(self):
        pillar = copy.deepcopy(PILLAR)
        settings = {
            "agent_enabled": True,
            "agent_default": "codex",
            "claude_model": "claude-test",
            "codex_model": "codex-test",
            "opencode_model": "provider/model",
            "agent_env": {"OPENAI_API_KEY": 'secret"with\\escapes'},
            "backup_enabled": True,
            "backup_bucket": "customer-backups",
            "backup_access_key": "access",
            "backup_secret_key": "secret",
            "backup_endpoint": "https://s3.example.com",
            "backup_region": "eu",
            "backup_prefix": "client",
            "backup_snapshot_time": "02:15",
            "backup_basebackup_time": "03:45",
            "backup_walg_keep_full": 8,
            "retention": ["1:14"],
            "routes": [
                {"name": "api", "host": "api.acme.example.com", "url": "http://127.0.0.1:3000"}
            ],
            "overlay_threshold_mb": 80,
            "workers_cap": 3,
            "trace": True,
            "disable_telemetry": False,
            "allow_local_path": False,
        }
        pillar["oduflow"].update(
            client_settings=settings, auto_stop_hours=4, auto_delete_hours=0, environment_slots=2
        )
        config = tomllib.loads(render("oduflow/files/oduflow.toml.jinja", pillar))
        self.assertEqual(config["team"]["1"]["agent_env"], settings["agent_env"])
        self.assertEqual(config["team"]["1"]["agent_default"], "codex")
        self.assertEqual(config["team"]["1"]["environment_slots"], 2)
        self.assertEqual(config["backup"]["keep"], ["1:14"])
        self.assertEqual(config["backup"]["secret_key"], "secret")
        self.assertEqual(config["route"]["api"]["host"], "api.acme.example.com")
        self.assertEqual(config["route"]["paseo"]["url"], "http://172.17.0.1:6768")
        self.assertFalse(config["server"]["allow_local_path"])
        self.assertFalse(config["server"]["allow_insecure_http"])
        self.assertEqual(config["production"]["workers_cap"], 3)
        self.assertEqual(config["lifecycle"]["auto_delete_hours"], 0)

    def test_legacy_defaults_and_disabled_backup_omit_credentials(self):
        config = tomllib.loads(render("oduflow/files/oduflow.toml.jinja"))
        self.assertNotIn("backup", config)
        self.assertFalse(config["team"]["1"]["agent_enabled"])
        self.assertTrue(config["server"]["disable_telemetry"])
        self.assertEqual(config["storage"]["data_dir"], "/srv/oduflow/data")

    def test_candidate_is_validated_before_replacing_secret_config(self):
        state = high("oduflow/init.sls")["oduflow-config"]["file.managed"]
        config = {key: value for entry in state for key, value in entry.items()}
        self.assertFalse(config["show_changes"])
        self.assertIn("oduflow-validate-oduflow", config["check_cmd"])
        self.assertIn({"file": "client-apps-oduflow-validate-helper"}, config["require"])
