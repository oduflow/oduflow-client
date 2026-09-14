"""Publication sequencing/recovery uses local receipts and mocked host operations."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "publisher",
    Path(__file__).resolve().parents[1] / "salt/states/client_production/files/publish.py",
)
pub = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pub)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.helper = Mock()
        self.helper.STATE_DIR = self.root
        self.helper.GUARD = self.root / "guard.json"
        self.helper.fingerprint.return_value = "production-fingerprint"
        self.helper.private_json.side_effect = lambda p: json.loads(p.read_text())
        self.helper.save_state.side_effect = lambda p, d: p.write_text(json.dumps(d))
        self.config = {"instance_uuid": "client", "domain": "client.example.org"}
        self.binding = {
            "instance_uuid": "client",
            "public_ip": "136.244.104.213",
            "interface": "enp1s0",
        }
        self.events = []
        self.guard = Mock()
        self.status = "open"
        self.guard.apply.side_effect = self.apply_guard
        self.is_hardened = False
        self.helper.execute.side_effect = self.create
        self.restart = lambda helper: self.events.append("restart")
        self.verify = lambda: self.events.append("verify")
        patcher = patch.object(pub, "hardened", side_effect=lambda *a: self.is_hardened)
        patcher.start()
        self.addCleanup(patcher.stop)

    def apply_guard(self, action, binding):
        self.events.append(action)
        if action == "close":
            self.status = "closed"
        elif action == "restore":
            self.assertTrue(self.is_hardened, "never restore before hardening")
            self.status = "open"
        return {
            "status": self.status,
            "ipv4": self.status == "closed",
            "ipv6": self.status == "closed",
        }

    def create(self, config, api, receipt):
        self.events.append("create")
        self.assertEqual(self.status, "closed")
        attestation = json.loads(self.helper.GUARD.read_text())
        self.assertTrue(attestation["public_ingress_closed"])
        self.assertEqual(attestation["guard_kind"], "host_raw")
        self.is_hardened = True
        return {"status": "hardened"}

    def execute(self):
        return pub.execute(
            self.helper,
            self.guard,
            self.config,
            self.binding,
            verify=self.verify,
            restart=self.restart,
        )

    def test_first_publish_orders_close_create_restore_restart_verify(self):
        result = self.execute()
        self.assertTrue(result["changed"])
        self.assertEqual(
            self.events, ["status", "close", "create", "restore", "status", "restart", "verify"]
        )
        self.assertFalse(self.helper.GUARD.exists())
        self.assertEqual(
            json.loads((self.root / "publication.json").read_text())["status"], "verified"
        )

    def test_existing_hardened_open_adoption_only_verifies(self):
        self.is_hardened = True
        self.assertFalse(self.execute()["changed"])
        self.assertEqual(self.events, ["status", "verify"])
        self.helper.execute.assert_not_called()
        self.helper.save_state.assert_not_called()

    def test_completed_rerun_does_not_reset_restart_or_close(self):
        self.execute()
        self.events.clear()
        self.assertFalse(self.execute()["changed"])
        self.assertEqual(self.events, ["status", "verify"])
        self.assertEqual(self.helper.execute.call_count, 1)

    def test_unknown_creation_keeps_closed_and_never_restores(self):
        self.helper.execute.side_effect = RuntimeError("secret-api-details")
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertEqual(self.status, "closed")
        self.assertNotIn("restore", self.events)
        self.assertNotIn("restart", self.events)
        self.assertEqual(
            json.loads((self.root / "publication.json").read_text())["status"], "closed"
        )

    def test_recovery_after_hardened_commit_uses_existing_instance(self):
        self.execute()
        record = json.loads((self.root / "publication.json").read_text())
        record["status"] = "closed"
        (self.root / "publication.json").write_text(json.dumps(record))
        self.status = "closed"
        self.events.clear()
        self.helper.execute.reset_mock()
        self.execute()
        self.helper.execute.assert_not_called()
        self.assertEqual(self.events, ["status", "restore", "status", "restart", "verify"])

    def test_verification_failure_does_not_repeat_create_or_restart(self):
        self.verify = Mock(side_effect=pub.SafeError("public_verification_pending"))
        with self.assertRaises(pub.SafeError):
            self.execute()
        self.assertEqual(self.status, "open")  # Already hardened and safely restored.
        self.verify = lambda: self.events.append("verify")
        self.events.clear()
        self.execute()
        self.assertEqual(self.events, ["status", "verify"])
        self.assertEqual(self.helper.execute.call_count, 1)

    def test_partial_close_cannot_create_or_publish(self):
        self.guard.apply.side_effect = lambda action, binding: {
            "status": "partial",
            "ipv4": True,
            "ipv6": False,
        }
        with self.assertRaisesRegex(pub.SafeError, "not_closed"):
            self.execute()
        self.helper.execute.assert_not_called()
        self.assertNotIn("restart", self.events)

    def test_foreign_publication_receipt_fails_before_firewall(self):
        (self.root / "publication.json").write_text(
            json.dumps({"identity": {"instance_uuid": "foreign"}})
        )
        with self.assertRaisesRegex(pub.SafeError, "identity_mismatch"):
            self.execute()
        self.guard.apply.assert_not_called()

    def test_boot_recloses_unfinished_guard_but_leaves_open_guard(self):
        self.guard.RECEIPT = self.root / "host-guard.json"
        self.guard.identity.return_value = self.binding
        for state in ("closing", "closed", "partial", "open", "restoring"):
            self.guard.RECEIPT.write_text(json.dumps({**self.binding, "status": state}))
            self.guard.apply.reset_mock()
            pub.boot_guard(self.helper, self.guard)
            self.assertEqual(
                self.guard.apply.call_count, int(state in ("closing", "closed", "partial"))
            )

    def test_public_verification_deadline_is_bounded(self):
        now = [0]

        def sleep(seconds):
            now[0] += seconds

        with patch.object(
            pub.subprocess, "run", return_value=Mock(returncode=1, stdout=b"{}")
        ) as run:
            with self.assertRaisesRegex(pub.SafeError, "verification_pending"):
                pub.verify_public(timeout=10, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(now[0], 10)
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
