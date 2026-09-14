"""Paseo project provisioning preserves working copies and retries registration safely."""

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_client_apps import PILLAR, args, high, load_module

project = load_module("paseo_project", "paseo/files/project.py")


def configured_pillar():
    pillar = copy.deepcopy(PILLAR)
    pillar["oduflow"]["git"] = {
        "host": "github.com",
        "username": "client",
        "repo": "oduflow/example-client",
        "branch": "main",
        "token": "fixture-client-token-12345",
    }
    pillar["paseo"]["github"] = {
        "repo": "https://github.com/oduflow/example-client",
        "token": "fixture-client-token-12345",
    }
    return pillar


class ProjectStates(unittest.TestCase):
    def test_repository_is_optional_but_partial_or_mismatched_config_fails(self):
        self.assertIn("paseo-project-not-configured", high("paseo/project.sls"))
        for section, key, value in [
            ("github", "repo", "https://github.com/oduflow/other"),
            ("github", "token", "different-client-token"),
            ("git", "repo", "oduflow/../other"),
            ("git", "branch", ""),
        ]:
            pillar = configured_pillar()
            pillar["paseo" if section == "github" else "oduflow"][section][key] = value
            self.assertEqual(
                list(high("paseo/project.sls", pillar)), ["paseo-project-invalid-pillar"]
            )

    def test_secret_is_private_and_project_gates_health_after_service_start(self):
        pillar = configured_pillar()
        data = high("paseo/project.sls", pillar)
        secret = args(data["paseo-github-credentials"])
        self.assertEqual(secret["mode"], "0600")
        self.assertFalse(secret["show_changes"])
        self.assertEqual(secret["user"], "paseo")
        config = json.loads(args(data["paseo-project-config"])["contents"])
        self.assertNotIn("token", config)
        self.assertEqual(config["repo"], "oduflow/example-client")
        health = high("client_apps/health.sls", pillar)
        self.assertIn("paseo.project", health["include"])
        self.assertIn(
            {"cmd": "paseo-project-ready"}, args(health["extend"]["client-apps-health"])["require"]
        )
        self.assertIn(
            {"service": "client-apps-paseo-running"},
            args(health["extend"]["paseo-project-storage"])["require"],
        )
        self.assertIn("gh", args(high("paseo/packages.sls")["paseo-github-cli"])["pkgs"])
        self.assertIn("paseo.packages", high("client_apps/install.sls", {})["include"])


class CheckoutPreservation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name)
        self.target = self.parent / "example-client"
        self.config = {"repo": "oduflow/example-client", "branch": "main"}
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.target),
                "remote",
                "add",
                "origin",
                "https://github.com/oduflow/example-client.git",
            ],
            check=True,
        )
        (self.target / "work.txt").write_text("uncommitted client work")

    def test_existing_checkout_is_not_fetched_reset_or_recreated(self):
        target, changed = project.checkout(self.config, self.parent, os.environ.copy())
        self.assertEqual(target, self.target)
        self.assertFalse(changed)
        self.assertEqual((target / "work.txt").read_text(), "uncommitted client work")

    def test_wrong_repository_and_symlink_are_refused(self):
        subprocess.run(
            [
                "git",
                "-C",
                str(self.target),
                "remote",
                "set-url",
                "origin",
                "https://github.com/oduflow/other.git",
            ],
            check=True,
        )
        with self.assertRaisesRegex(project.SafeError, "repository_mismatch"):
            project.checkout(self.config, self.parent, os.environ.copy())
        self.target.rename(self.parent / "other")
        self.target.symlink_to(self.parent / "other")
        with self.assertRaisesRegex(project.SafeError, "directory_unsafe"):
            project.checkout(self.config, self.parent, os.environ.copy())

    def test_existing_project_and_lost_create_response_do_not_duplicate(self):
        row = {"path": str(self.target), "projectId": "existing"}
        with patch.object(project, "run", return_value=json.dumps([row])) as run:
            self.assertFalse(project.ensure_project(Path("/bin/paseo"), self.target, {}))
            self.assertEqual(run.call_count, 1)
        with patch.object(project, "run", side_effect=["[]", "{}", json.dumps([row])]) as run:
            self.assertTrue(project.ensure_project(Path("/bin/paseo"), self.target, {}))
            self.assertEqual(run.call_count, 3)
        # A retry after create succeeded but its response was lost starts by listing.
        with patch.object(project, "run", return_value=json.dumps([row])) as run:
            self.assertFalse(project.ensure_project(Path("/bin/paseo"), self.target, {}))
            self.assertEqual(run.call_count, 1)
