"""Repository transport, credential isolation and Salt rendering contracts."""

import copy
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import salt.config
import salt.state
from test_client_apps import PILLAR, UUID, args, high, render


def pillar():
    value = copy.deepcopy(PILLAR)
    value["github_downloads"] = [
        {
            "repository": "oduflow/oduflow",
            "identity": UUID,
            "private_key": (
                "-----BEGIN OPENSSH PRIVATE KEY-----\nfixture\n-----END OPENSSH PRIVATE KEY-----\n"
            ),
        }
    ]
    value["oduflow"]["git"] = {
        "host": "github.com",
        "username": "git",
        "repo": "oduflow/acme",
        "branch": "main",
        "auth": "ssh",
    }
    value["oduflow"]["production_admin_password"] = "x" * 24
    value["paseo"]["github"] = {"repo": "https://github.com/oduflow/acme", "auth": "ssh"}
    return value


class GitHubDownloadContracts(unittest.TestCase):
    def test_keys_stay_on_verified_volume_with_private_modes_and_hidden_changes(self):
        data = high("github_downloads/init.sls", pillar())
        for user in ("root", "paseo"):
            key = args(data[f"github-downloads-key-{user}-{UUID}"])
            self.assertEqual(key["mode"], "0600")
            self.assertEqual(key["user"], user)
            self.assertFalse(key["show_changes"])
            self.assertTrue(key["name"].startswith("/srv/oduflow/data/"))
            directory = args(data[f"github-downloads-directory-{user}"])
            self.assertIn({"cmd": "oduflow-storage-verify"}, directory["require"])

    def test_invalid_identity_or_path_cannot_render_credentials(self):
        for field, invalid in (
            ("repository", "a/b\nHost *"),
            ("identity", "../../root"),
            ("private_key", "invalid"),
        ):
            value = pillar()
            value["github_downloads"][0][field] = invalid
            self.assertEqual(
                list(high("github_downloads/init.sls", value)), ["github-downloads-invalid-pillar"]
            )
        value = pillar()
        value["storage"]["device"] = "/dev/sda"
        self.assertEqual(
            list(high("github_downloads/init.sls", value)), ["github-downloads-invalid-pillar"]
        )

    def test_ssh_client_bootstrap_has_no_token_requirement(self):
        value = pillar()
        data = high("paseo/project.sls", value)
        self.assertIn("file.absent", data["paseo-github-credentials"])
        self.assertIn({"sls": "github_downloads"}, args(data["paseo-project-ready"])["require"])
        data = high("client_production/init.sls", value)
        self.assertIn("file.absent", data["client-production-git-token"])
        config = json.loads(args(data["client-production-config"])["contents"])
        self.assertIsNone(config["git_token_file"])

    def test_git_rewrites_only_configured_repository_to_its_ssh_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "gitconfig"
            config.write_text(render("github_downloads/files/gitconfig.jinja", pillar()))
            env = dict(os.environ, GIT_CONFIG_SYSTEM=str(config), GIT_CONFIG_GLOBAL=os.devnull)
            for url in (
                "https://github.com/oduflow/oduflow.git",
                "git@github.com:oduflow/oduflow.git",
            ):
                result = subprocess.run(
                    ["git", "ls-remote", "--get-url", url],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(
                    result.stdout.strip(), f"git@oduflow-download-{UUID}:oduflow/oduflow.git"
                )
            url = "https://github.com/oduflow/another-client.git"
            result = subprocess.run(
                ["git", "ls-remote", "--get-url", url],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(result.stdout.strip(), url)

    def test_ssh_uses_pinned_host_and_only_the_selected_key(self):
        value = render("github_downloads/files/ssh_config.jinja", pillar())
        self.assertIn("StrictHostKeyChecking yes", value)
        self.assertIn("IdentitiesOnly yes", value)
        self.assertIn(f"/srv/oduflow/data/github-downloads/%u/{UUID}", value)
        self.assertNotIn("PRIVATE KEY", value)

    def test_real_salt_compiles_client_role_with_repository_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "minion"
            config.write_text(
                json.dumps(
                    {
                        "file_client": "local",
                        "file_roots": {
                            "base": [str(Path(__file__).resolve().parents[1] / "salt/states")]
                        },
                        "cachedir": root + "/cache",
                        "pki_dir": root + "/pki",
                        "sock_dir": root + "/sock",
                        "log_file": root + "/log",
                        "id": "client-" + UUID,
                        "grains": {
                            "id": "client-" + UUID,
                            "os": "Ubuntu",
                            "os_family": "Debian",
                            "cpuarch": "x86_64",
                            "osarch": "amd64",
                        },
                    }
                )
            )
            with salt.state.HighState(
                salt.config.minion_config(str(config)), initial_pillar=pillar()
            ) as compiler:
                data, errors = compiler.render_highstate({"base": ["roles.client_stack"]})
                self.assertEqual(errors, [])
                self.assertEqual(compiler.state.verify_high(data), [])
                low = compiler.state.compile_high_data(data)
                self.assertTrue(low)
                self.assertIn("github-downloads-key-root-" + UUID, data)
                self.assertIn("github-downloads-key-paseo-" + UUID, data)
                # Verify explicit requisites against the complete compiled role.
                identifiers = {(item["state"], item["__id__"]) for item in low}
                for item in low:
                    if item["__id__"].startswith("github-downloads-"):
                        for requirement in item.get("require", []):
                            for state, identifier in requirement.items():
                                self.assertIn((state, identifier), identifiers)


class RetireGitHubToken(unittest.TestCase):
    def test_retirement_preserves_other_hosts_is_idempotent_and_rejects_symlinks(self):
        path = Path(__file__).parents[1] / "salt/states/github_downloads/files/retire-token.py"
        spec = importlib.util.spec_from_file_location("retire_github_token", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            module.STORE = Path(folder) / "credentials"
            module.TOKEN = Path(folder) / "token"
            module.STORE.write_text("https://user:secret@github.com\nhttps://u:p@example.org\n")
            module.TOKEN.write_text("legacy-token")
            module.STORE.chmod(0o600)
            module.TOKEN.chmod(0o600)
            self.assertTrue(module.retire())
            self.assertEqual(module.STORE.read_text(), "https://u:p@example.org\n")
            self.assertFalse(module.TOKEN.exists())
            self.assertFalse(module.retire())
            module.TOKEN.symlink_to(module.STORE)
            with self.assertRaises(ValueError):
                module.retire()

    def test_cleanup_requires_own_repository_ssh_access(self):
        value = pillar()
        self.assertNotIn("github-downloads-retire-token", high("github_downloads/init.sls", value))
        value["github_downloads"][0]["repository"] = value["oduflow"]["git"]["repo"]
        data = high("github_downloads/init.sls", value)
        self.assertIn(
            {"cmd": "oduflow-storage-verify"},
            args(data["github-downloads-retire-token"])["require"],
        )
