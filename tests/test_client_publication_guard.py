import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "publication_guard", ROOT / "scripts/client-publication-guard.py"
)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
BINDING = {
    "instance_uuid": "1d378f52-e6bf-49cc-b719-b6323c5ddd4c",
    "public_ip": "136.244.104.213",
    "interface": "enp1s0",
}


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        patch.object(guard, "DIRECTORY", directory).start()
        patch.object(guard, "RECEIPT", directory / "receipt.json").start()
        self.addCleanup(patch.stopall)
        self.rules = set()
        self.commands = []

        def rule(binary, action, binding):
            self.assertEqual(binding, BINDING)
            self.commands.append((binary, action))
            if action == "-I":
                self.rules.add(binary)
            if action == "-D":
                self.rules.remove(binary)
            return binary in self.rules if action == "-C" else True

        patch.object(guard, "rule", side_effect=rule).start()

    def test_close_idempotent_and_restore_only_owned_rules(self):
        self.assertEqual(guard.apply("close", BINDING)["status"], "closed")
        self.assertEqual(guard.apply("close", BINDING)["status"], "closed")
        self.assertEqual(sum(action == "-I" for _, action in self.commands), 2)
        self.assertEqual(guard.RECEIPT.stat().st_mode & 0o777, 0o600)
        self.assertEqual(guard.apply("restore", BINDING)["status"], "open")
        self.assertEqual(sum(action == "-D" for _, action in self.commands), 2)
        self.assertEqual(guard.apply("restore", BINDING)["status"], "open")

    def test_foreign_receipt_refused_before_mutation(self):
        guard.apply("close", BINDING)
        self.commands.clear()
        with self.assertRaisesRegex(ValueError, "another interface"):
            guard.apply("restore", {**BINDING, "interface": "tailscale0"})
        self.assertEqual(self.commands, [])

    def test_missing_receipt_cannot_restore_and_status_is_read_only(self):
        with self.assertRaisesRegex(ValueError, "without an owned"):
            guard.apply("restore", BINDING)
        self.assertEqual(guard.apply("status", BINDING)["status"], "open")
        self.assertFalse(guard.RECEIPT.exists())

    def test_partial_failure_retains_claim_and_blocks_success(self):
        with patch.object(guard, "rule", side_effect=ValueError("iptables failed")):
            with self.assertRaises(ValueError):
                guard.apply("close", BINDING)
        self.assertEqual(json.loads(guard.RECEIPT.read_text())["status"], "closing")
