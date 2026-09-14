"""Optional coding-agent installation/configuration contracts; no model calls."""

import copy
import json
import unittest

from test_client_apps import PILLAR, args, high, render


def configured_pillar():
    pillar = copy.deepcopy(PILLAR)
    pillar["paseo"]["llm"] = {
        "key": "sk-fixture-virtual-key-not-real",
        "base_url": "https://llm.example.org/v1",
        "models": ["coding-model", "vendor/other-coding-model"],
    }
    return pillar


class ClientAgentContracts(unittest.TestCase):
    def test_client_mcp_uses_environment_token_and_client_endpoint(self):
        pillar = configured_pillar()
        config = json.loads(render("client_agent/files/opencode.json.jinja", pillar))
        self.assertEqual(
            config["mcp"]["oduflow"],
            {
                "type": "remote",
                "url": "https://oduflow.acme.example.com/mcp",
                "enabled": True,
                "oauth": False,
                "headers": {"Authorization": "Bearer {env:ODUFLOW_MCP_TOKEN}"},
            },
        )
        self.assertNotIn(pillar["oduflow"]["auth_token"], json.dumps(config))
        environment = args(high("paseo/init.sls", pillar)["paseo-password"])
        self.assertEqual(environment["mode"], "0600")
        self.assertEqual(environment["user"], "root")
        self.assertFalse(environment["show_changes"])
        self.assertIn(
            "ODUFLOW_MCP_TOKEN=" + pillar["oduflow"]["auth_token"], environment["contents"]
        )
        pillar["oduflow"]["auth_token"] = "unsafe-token-value\nINJECTED=value"
        self.assertIn("paseo-invalid-pillar", high("paseo/init.sls", pillar))

    def test_absent_llm_installs_nothing(self):
        data = high("client_agent/init.sls")
        self.assertEqual(list(data), ["client-agent-not-configured"])
        self.assertIn("test.nop", data["client-agent-not-configured"])

    def test_partial_invalid_or_injected_llm_fails_without_install(self):
        for key, invalid in (
            ("key", "master-key"),
            ("base_url", "http://llm.example.org/v1"),
            ("base_url", "https://user:password@llm.example.org/v1"),
            ("models", []),
            ("models", "coding-model"),
            ("models", ["{file:/etc/shadow}"]),
            ("models", [None]),
        ):
            pillar = configured_pillar()
            pillar["paseo"]["llm"][key] = invalid
            data = high("client_agent/init.sls", pillar)
            self.assertEqual(list(data), ["client-agent-invalid-pillar"])
            start = high("client_apps/start.sls", pillar)
            self.assertIn(
                {"test": "client-agent-invalid-pillar"},
                args(start["client-apps-paseo-running"])["require"],
            )

    def test_http_is_allowed_only_for_literal_tailscale_ipv4(self):
        for url in (
            "http://100.64.0.4:4000/v1",
            "http://100.64.0.0/v1",
            "http://100.127.255.255:65535/v1",
        ):
            with self.subTest(url=url):
                pillar = configured_pillar()
                pillar["paseo"]["llm"]["base_url"] = url
                data = high("client_agent/init.sls", pillar)
                self.assertIn("client-agent-config", data)
                config = json.loads(render("client_agent/files/opencode.json.jinja", pillar))
                self.assertEqual(config["provider"]["oduflow"]["options"]["baseURL"], url)
                self.assertNotIn(pillar["paseo"]["llm"]["key"], json.dumps(config))

    def test_http_dns_other_networks_or_ambiguous_ipv4_are_rejected(self):
        for url in (
            "http://litellm:4000/v1",
            "http://llm.example.org:4000/v1",
            "http://100.63.255.255:4000/v1",
            "http://100.128.0.0:4000/v1",
            "http://100.64.256.1:4000/v1",
            "http://100.64.0.256:4000/v1",
            "http://100.064.0.4:4000/v1",
            "http://100.64.00.4:4000/v1",
            "http://100.64.0.4.example.org:4000/v1",
            "http://127.0.0.1:4000/v1",
            "http://10.0.0.4:4000/v1",
            "http://192.168.1.4:4000/v1",
            "http://[fd7a:115c:a1e0::4]:4000/v1",
            "http://[::ffff:100.64.0.4]:4000/v1",
            "http://1681915908:4000/v1",
            "http://100.64.0.4:0/v1",
            "http://100.64.0.4:65536/v1",
            "http://user:password@100.64.0.4:4000/v1",
            "http://100.64.0.4:4000/v1?key=secret",
            "http://100.64.0.4:4000/v1#fragment",
            "http://100.64.0.4:4000/v1\n",
        ):
            with self.subTest(url=url):
                pillar = configured_pillar()
                pillar["paseo"]["llm"]["base_url"] = url
                self.assertEqual(
                    list(high("client_agent/init.sls", pillar)), ["client-agent-invalid-pillar"]
                )
                config = json.loads(render("client_agent/files/opencode.json.jinja", pillar))
                self.assertEqual(config, {"invalid_client_configuration": True})

    def test_pinned_binary_is_on_paseo_path_and_secrets_are_private(self):
        data = high("client_agent/init.sls", configured_pillar())
        package = args(data["client-agent-package"])
        self.assertIn("1.14.46", package["source"])
        self.assertRegex(package["source_hash"], r"^sha512=[0-9a-f]{128}$")
        binary = args(data["client-agent-binary"])
        self.assertEqual(binary["name"], "/usr/local/bin/opencode")
        self.assertFalse(binary["force"])
        for name in ("client-agent-key", "client-agent-config"):
            private = args(data[name])
            self.assertEqual(private["user"], "paseo")
            self.assertEqual(private["mode"], "0600")
            self.assertFalse(private["show_changes"])
        self.assertIn(
            {"cmd": "oduflow-storage-verify"},
            args(data["client-agent-config-directory"])["require"],
        )

    def test_only_explicit_models_custom_chat_provider_no_embedded_key(self):
        pillar = configured_pillar()
        config = json.loads(render("client_agent/files/opencode.json.jinja", pillar))
        self.assertEqual(config["enabled_providers"], ["oduflow"])
        self.assertEqual(config["model"], "oduflow/coding-model")
        self.assertEqual(config["small_model"], config["model"])
        self.assertFalse(config["autoupdate"])
        self.assertEqual(config["share"], "disabled")
        provider = config["provider"]["oduflow"]
        self.assertEqual(provider["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(list(provider["models"]), pillar["paseo"]["llm"]["models"])
        self.assertEqual(provider["whitelist"], pillar["paseo"]["llm"]["models"])
        self.assertEqual(provider["options"]["baseURL"], "https://llm.example.org/v1")
        self.assertNotIn(pillar["paseo"]["llm"]["key"], json.dumps(config))
        self.assertEqual(
            provider["options"]["apiKey"], "{file:/srv/paseo/.config/opencode/litellm.key}"
        )

    def test_explicit_default_uses_responses_and_medium_reasoning(self):
        pillar = configured_pillar()
        pillar["paseo"]["llm"].update(
            {
                "base_url": "http://100.64.0.4:4000/v1",
                "models": ["another-model", "gpt-5.6-sol"],
                "default_model": "gpt-5.6-sol",
                "reasoning_effort": "medium",
                "api_mode": "responses",
            }
        )
        config = json.loads(render("client_agent/files/opencode.json.jinja", pillar))
        self.assertEqual(config["model"], "oduflow/gpt-5.6-sol")
        self.assertEqual(config["small_model"], config["model"])
        provider = config["provider"]["oduflow"]
        self.assertEqual(provider["npm"], "@ai-sdk/openai")
        self.assertEqual(provider["options"]["baseURL"], "http://100.64.0.4:4000/v1")
        selected = provider["models"]["gpt-5.6-sol"]
        self.assertTrue(selected["reasoning"])
        self.assertEqual(selected["options"], {"reasoningEffort": "medium", "store": False})
        self.assertEqual(provider["models"]["another-model"], {"name": "another-model"})
        self.assertEqual(provider["whitelist"], ["another-model", "gpt-5.6-sol"])
        self.assertNotIn(pillar["paseo"]["llm"]["key"], json.dumps(config))

    def test_legacy_pillar_preserves_chat_transport_and_defaults_to_medium(self):
        config = json.loads(render("client_agent/files/opencode.json.jinja", configured_pillar()))
        provider = config["provider"]["oduflow"]
        self.assertEqual(provider["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(config["model"], "oduflow/coding-model")
        self.assertEqual(
            provider["models"]["coding-model"]["options"], {"reasoningEffort": "medium"}
        )

    def test_invalid_model_reasoning_or_transport_prevents_agent_apply(self):
        for key, value in (
            ("default_model", "unlisted-model"),
            ("default_model", None),
            ("default_model", ["coding-model"]),
            ("reasoning_effort", "arbitrary-effort"),
            ("reasoning_effort", "xhigh"),
            ("reasoning_effort", "{file:/etc/shadow}"),
            ("reasoning_effort", None),
            ("reasoning_effort", True),
            ("api_mode", "arbitrary-sdk"),
            ("api_mode", "{file:/etc/shadow}"),
            ("api_mode", None),
        ):
            with self.subTest(key=key, value=value):
                pillar = configured_pillar()
                pillar["paseo"]["llm"][key] = value
                self.assertEqual(
                    list(high("client_agent/init.sls", pillar)), ["client-agent-invalid-pillar"]
                )
                self.assertEqual(
                    json.loads(render("client_agent/files/opencode.json.jinja", pillar)),
                    {"invalid_client_configuration": True},
                )

    def test_paseo_start_waits_for_agent_and_restarts_on_configuration_change(self):
        data = high("client_apps/start.sls", configured_pillar())
        self.assertIn("client_agent", data["include"])
        service = args(data["client-apps-paseo-running"])
        self.assertIn({"file": "client-agent-config"}, service["require"])
        self.assertIn({"file": "client-agent-binary"}, service["require"])
        self.assertIn({"file": "client-agent-key"}, service["watch"])
        self.assertIn({"file": "client-agent-config"}, service["watch"])


if __name__ == "__main__":
    unittest.main()
