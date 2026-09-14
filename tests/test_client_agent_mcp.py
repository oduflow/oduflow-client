"""Optional CLI installation and preserving MCP configuration contracts."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from test_client_apps import args, high, load_module

mcp = load_module("agent_mcp", "client_agent/files/mcp.py")


class AgentMCPTests(unittest.TestCase):
    def test_cli_install_is_credential_free_and_in_image_stage(self):
        states = high("client_agent/cli.sls", {})
        self.assertIn("client_agent.cli", high("client_apps/install.sls", {})["include"])
        for name in ("codex", "claude"):
            artifact = args(states[f"client-agent-{name}-package"])
            self.assertTrue(artifact["source"].startswith("https://registry.npmjs.org/"))
            self.assertRegex(artifact["source_hash"], r"^sha512=[a-f0-9]{128}$")
            self.assertEqual(artifact["user"], "root")
        self.assertFalse(any("service.running" in state for state in states.values()))

    def test_client_identity_required_and_service_waits_for_config(self):
        self.assertIn("client-agent-mcp-invalid-pillar", high("client_agent/mcp.sls", {}))
        config = args(high("client_agent/mcp.sls")["client-agent-mcp-config"])
        self.assertEqual(config["runas"], "paseo")
        self.assertIn("https://oduflow.acme.example.com/mcp", config["name"])
        self.assertIn(
            {"cmd": "client-agent-mcp-config"},
            args(high("client_apps/start.sls")["client-apps-paseo-running"])["require"],
        )

    def test_claude_merge_preserves_user_state_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".claude.json"
            original = {
                "numStartups": 12,
                "mcpServers": {"other": {"type": "stdio", "command": "other"}},
                "projects": {"/repo": {"hasTrustDialogAccepted": True}},
            }
            path.write_text(json.dumps(original))
            url = "https://oduflow.client.example/mcp"
            self.assertTrue(mcp.claude_config(path, url))
            result = json.loads(path.read_text())
            for key in ("numStartups", "projects"):
                self.assertEqual(result[key], original[key])
            self.assertEqual(result["mcpServers"]["other"], original["mcpServers"]["other"])
            self.assertEqual(
                result["mcpServers"]["oduflow"]["headers"]["Authorization"],
                "Bearer ${ODUFLOW_MCP_TOKEN}",
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            before = path.stat().st_mtime_ns
            self.assertFalse(mcp.claude_config(path, url))
            self.assertEqual(path.stat().st_mtime_ns, before)
            self.assertTrue(mcp.claude_config(path, "https://new.example/mcp"))

    def test_invalid_json_and_symlinks_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".claude.json"
            path.write_text("invalid json")
            with self.assertRaises(ValueError):
                mcp.claude_config(path, "https://test.example/mcp")
            self.assertEqual(path.read_text(), "invalid json")
            path.unlink()
            target = Path(temporary) / "target"
            target.write_text("{}")
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                mcp.claude_config(path, "https://test.example/mcp")
            self.assertEqual(target.read_text(), "{}")

    @unittest.skipUnless(os.environ.get("TEST_CODEX_BINARY"), "requires the pinned Codex binary")
    def test_real_codex_preserves_settings_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / ".codex").mkdir()
            config = home / ".codex/config.toml"
            config.write_text(
                '# User setting\nmodel = "user-model"\n[mcp_servers.other]\nurl = "https://other.example/mcp"\n'
            )
            binary = os.environ["TEST_CODEX_BINARY"]
            self.assertTrue(mcp.codex_config(home, "https://test.example/mcp", binary))
            self.assertIn('model = "user-model"', config.read_text())
            self.assertIn("[mcp_servers.other]", config.read_text())
            self.assertIn("# User setting", config.read_text())
            self.assertIn('bearer_token_env_var = "ODUFLOW_MCP_TOKEN"', config.read_text())
            before = config.read_bytes()
            self.assertFalse(mcp.codex_config(home, "https://test.example/mcp", binary))
            self.assertEqual(config.read_bytes(), before)
            self.assertTrue(mcp.codex_config(home, "https://new.example/mcp", binary))
