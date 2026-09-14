"""Verify the Salt-to-file boundary preserves JSON for the SSH helper."""

import json
import unittest
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]


class ClientSSHState(unittest.TestCase):
    def test_managed_configuration_is_json_text_with_exact_identity(self):
        pillar = {
            "instance_uuid": "0d23ddc9-eb7b-4483-9162-31886819bde9",
            "oduflow": {"ssh": {"ca_public_key": "ssh-ed25519 Zml4dHVyZQ=="}},
        }
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(ROOT / "salt/states"))
        env.filters["json"] = json.dumps
        rendered = env.get_template("client_ssh/init.sls").render(
            pillar=pillar, salt={"pillar.get": lambda key, default: pillar["oduflow"]["ssh"]}
        )
        state = yaml.safe_load(rendered)["client-ssh-config"]["file.managed"]
        args = {key: value for item in state for key, value in item.items()}
        self.assertIsInstance(args["contents"], str)
        self.assertEqual(
            json.loads(args["contents"]),
            {"instance_uuid": pillar["instance_uuid"], **pillar["oduflow"]["ssh"]},
        )


if __name__ == "__main__":
    unittest.main()
