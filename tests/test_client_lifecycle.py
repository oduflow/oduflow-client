"""Lifecycle failure recovery preserves workload identity and service ownership."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "client_lifecycle", ROOT / "salt/states/client_lifecycle/files/lifecycle.py"
)
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)
UID = "683a74a5-9ef6-4512-995a-9d6338c008bc"
CID = "a" * 64


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.units = set(lifecycle.UNITS) - {"paseo-proxy.service"}
        self.original = set(self.units)
        self.running = {CID}
        self.commands = []
        self.fail_stop = False
        self.fail_health = False
        for name, value in {
            "ROOT": root / "receipt",
            "MARKER": root / "receipt/suspended",
            "RECEIPT": root / "receipt/receipt.json",
            "SYSTEMD": root / "systemd",
        }.items():
            p = patch.object(lifecycle, name, value)
            p.start()
            self.addCleanup(p.stop)
        for p in (
            patch.object(lifecycle, "verify_storage"),
            patch.object(lifecycle, "run", self.run_command),
        ):
            p.start()
            self.addCleanup(p.stop)

    def run_command(self, *args, check=True):
        self.commands.append(args)
        output = ""
        code = 0
        if args[:2] == ("systemctl", "is-active"):
            code = 0 if args[-1] in self.units else 3
        elif args[:2] == ("systemctl", "stop"):
            if self.fail_stop and args[-1] == "docker.service":
                self.fail_stop = False
                raise lifecycle.LifecycleError("stop failed")
            self.units.discard(args[-1])
        elif args[:2] == ("systemctl", "start"):
            if lifecycle.MARKER.exists():
                raise lifecycle.LifecycleError("guard blocked service startup")
            self.units.add(args[-1])
        elif args[:2] == ("docker", "ps"):
            output = "\n".join(self.running)
        elif args[:2] == ("docker", "stop"):
            self.running.difference_update(args[4:])
        elif args[:2] == ("docker", "start"):
            self.running.update(args[2:])
        elif args == ("/usr/local/libexec/oduflow-client-health",) and self.fail_health:
            raise lifecycle.LifecycleError("health failed")
        return SimpleNamespace(stdout=output, returncode=code)

    def test_suspend_resume_preserves_exact_workloads_and_guards_reboot(self):
        lifecycle.apply(UID, 1, "suspend")
        self.assertFalse(self.units)
        self.assertFalse(self.running)
        self.assertTrue(lifecycle.MARKER.exists())
        for unit in lifecycle.UNITS:
            self.assertEqual(
                (lifecycle.SYSTEMD / (unit + ".d") / "90-oduflow-lifecycle.conf").read_text(),
                lifecycle.GUARD,
            )
        lifecycle.apply(UID, 1, "resume")
        self.assertEqual(self.units, self.original)
        self.assertEqual(self.running, {CID})
        self.assertFalse(lifecycle.MARKER.exists())
        self.assertFalse(
            any(
                "salt-minion" in str(cmd) or "tailscale" in str(cmd) or "restic" in str(cmd)
                for cmd in self.commands
            )
        )

    def test_partial_suspend_retry_keeps_original_inventory(self):
        self.fail_stop = True
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.apply(UID, 1, "suspend")
        self.assertTrue(lifecycle.MARKER.exists())
        lifecycle.apply(UID, 1, "suspend")
        lifecycle.apply(UID, 1, "resume")
        self.assertEqual(self.units, self.original)
        self.assertEqual(self.running, {CID})

    def test_failed_resume_does_not_claim_active_and_can_retry(self):
        lifecycle.apply(UID, 1, "suspend")
        self.fail_health = True
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.apply(UID, 1, "resume")
        self.assertEqual(json.loads(lifecycle.RECEIPT.read_text())["phase"], "resuming")
        self.fail_health = False
        lifecycle.apply(UID, 1, "resume")
        self.assertEqual(json.loads(lifecycle.RECEIPT.read_text())["phase"], "active")

    def test_stale_suspend_cannot_stop_resumed_client(self):
        lifecycle.apply(UID, 1, "suspend")
        lifecycle.apply(UID, 1, "resume")
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.apply(UID, 1, "suspend")
        self.assertEqual(self.units, self.original)
        lifecycle.apply(UID, 2, "suspend")
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.apply(UID, 1, "resume")
        self.assertFalse(self.units)

    def test_foreign_storage_blocks_every_mutation(self):
        with patch.object(lifecycle, "verify_storage", side_effect=ValueError("foreign")):
            with self.assertRaises(ValueError):
                lifecycle.apply(UID, 1, "suspend")
        self.assertFalse(self.commands)
        self.assertFalse(lifecycle.ROOT.exists())

    def test_resume_without_suspension_refuses_to_start_services(self):
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.apply(UID, 1, "resume")
        self.assertFalse(self.commands)

    def test_existing_foreign_guard_is_preserved(self):
        guard = lifecycle.SYSTEMD / "oduflow.service.d/90-oduflow-lifecycle.conf"
        guard.parent.mkdir(parents=True)
        guard.write_text("foreign configuration")
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.apply(UID, 1, "suspend")
        self.assertEqual(guard.read_text(), "foreign configuration")
        self.assertEqual(self.units, self.original)
