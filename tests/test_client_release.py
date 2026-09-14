"""Real Git and Salt checks for immutable checkout and isolated state application."""

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import salt.config
import salt.loader

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "client_release", ROOT / "salt/minion/extmods/modules/oduflow_release.py"
)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
MINION = "client-12345678-1234-1234-1234-123456789abc"


class ClientReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git("init", "--quiet")
        # Test-only synthetic repository; these commits are not user history.
        self.git("config", "user.name", "Test Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.source / "release.json").write_text('{"contract":1,"salt":"3006.27"}')
        role = self.source / "salt/states/roles/client_stack.sls"
        role.parent.mkdir(parents=True)
        role.write_text("local-release:\n  test.nop:\n    - name: '{{ pillar.secret }}'\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "Fixture")
        self.commit = self.git("rev-parse", "HEAD")
        for key, value in {
            "ROOT": self.root / "releases",
            "REPOSITORY": str(self.source),
            "_OWNER_UID": os.getuid(),
            "_RUN_ROOT": str(self.root),
        }.items():
            patcher = patch.object(release, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.source), *args], text=True).strip()

    def test_exact_checkout_reuse_and_dirty_checkout_refusal(self):
        target = Path(release.checkout(self.commit))
        self.assertEqual(release.checkout(self.commit), str(target))
        (target / "release.json").write_text("changed")
        with self.assertRaisesRegex(ValueError, "modified"):
            release.checkout(self.commit)
        self.assertEqual((target / "release.json").read_text(), "changed")

    def test_moving_branch_does_not_change_selected_revision(self):
        (self.source / "next.txt").write_text("later release")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "Later fixture")
        target = Path(release.checkout(self.commit))
        self.assertFalse((target / "next.txt").exists())
        for value in ("main", "../release", "a" * 39, None):
            with self.assertRaisesRegex(ValueError, "revision_invalid"):
                release.checkout(value)

    def test_local_salt_uses_checkout_and_keeps_pillar_out_of_config(self):
        opts = salt.config.minion_config(None)
        opts.update(
            id=MINION,
            file_client="local",
            file_roots={"base": []},
            cachedir=str(self.root / "cache"),
            pki_dir=str(self.root / "pki"),
            sock_dir=str(self.root / "sock"),
            pillar_roots={"base": []},
            grains={},
            cache_jobs=False,
            minion_pillar_cache=False,
        )
        functions = salt.loader.minion_mods(opts, utils=salt.loader.utils(opts))
        release.__salt__ = {
            "pillar.items": lambda: {
                "instance_uuid": MINION.removeprefix("client-"),
                "secret": "PRIVATE-CANARY",
            }
        }
        with release.local_options(self.commit, MINION) as options:
            config = Path(options["localconfig"])
            self.assertNotIn("PRIVATE-CANARY", config.read_text())
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            result = functions["state.apply"]("roles.client_stack", **options)
            self.assertTrue(result)
            self.assertTrue(all(value["result"] for value in result.values()), result)
        self.assertFalse(config.exists())
        release.__salt__["pillar.items"] = lambda: {"instance_uuid": "foreign"}
        with self.assertRaisesRegex(ValueError, "pillar_identity"):
            with release.local_options(self.commit, MINION):
                self.fail("Foreign pillar accepted")
