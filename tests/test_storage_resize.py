"""Online XFS growth safety; all kernel/filesystem mutations are mocked."""

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "grow_storage",
    Path(__file__).resolve().parents[1] / "salt/states/client_storage_resize/files/grow.py",
)
grow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(grow)
UUID = "1d378f52-e6bf-49cc-b719-b6323c5ddd4c"


class GrowTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "instance_uuid": UUID,
            "request_id": UUID,
            "device": "/dev/disk/by-id/virtio-owned",
            "target_size_gb": 80,
        }
        self.owner = {
            "instance_uuid": UUID,
            "device": self.config["device"],
            "filesystem_uuid": UUID,
        }
        self.storage = Mock()
        self.commands = []
        self.blocks = 50 * 1024**3 // 4096

        def command(*args):
            self.commands.append(args)
            if args[0] == "blockdev":
                return str(80 * 1024**3)
            if args[0] == "xfs_growfs":
                self.blocks = int(args[2])
                return ""
            raise AssertionError(args)

        for item in (
            patch.object(grow, "private_json", return_value=self.owner),
            patch.object(Path, "resolve", return_value=Path("/dev/vdb")),
            patch.object(Path, "exists", return_value=False),
            patch.object(grow, "geometry", side_effect=lambda: (4096, self.blocks)),
            patch.object(grow, "run", side_effect=command),
        ):
            item.start()
            self.addCleanup(item.stop)

    def test_grow_keeps_mount_and_services_and_never_formats(self):
        self.assertTrue(grow.grow(self.config, self.storage)["changed"])
        self.assertEqual(
            self.commands[-1], ("xfs_growfs", "-D", str(80 * 1024**3 // 4096), "/srv/oduflow/data")
        )
        self.assertEqual(self.storage.prepare.call_count, 3)
        for call in self.storage.prepare.call_args_list:
            self.assertTrue(call.args[0].verify)
            self.assertFalse(call.args[0].allow_format)
        self.assertFalse(
            any(cmd[0] in ("mkfs.xfs", "umount", "systemctl", "mount") for cmd in self.commands)
        )

    def test_already_grown_request_is_read_only_idempotent(self):
        self.blocks = 80 * 1024**3 // 4096
        self.assertFalse(grow.grow(self.config, self.storage)["changed"])
        self.assertFalse(any(cmd[0] == "xfs_growfs" for cmd in self.commands))

    def test_foreign_owner_refuses_before_rescan_or_grow(self):
        self.owner["instance_uuid"] = "foreign"
        with self.assertRaisesRegex(grow.UnsafeResize, "identity_mismatch"):
            grow.grow(self.config, self.storage)
        self.storage.prepare.assert_not_called()
        self.assertFalse(self.commands)

    def test_readonly_or_wrong_mount_stops_before_mutation(self):
        self.storage.prepare.side_effect = RuntimeError("unsafe mount")
        with self.assertRaises(RuntimeError):
            grow.grow(self.config, self.storage)
        self.assertFalse(self.commands)

    def test_shrink_and_unexpected_bigger_device_fail_closed(self):
        self.config["target_size_gb"] = 40
        with self.assertRaisesRegex(grow.UnsafeResize, "filesystem_larger"):
            grow.grow(self.config, self.storage)
        self.config["target_size_gb"] = 60
        with self.assertRaisesRegex(grow.UnsafeResize, "device_larger"):
            grow.grow(self.config, self.storage)
        self.assertFalse(any(cmd[0] == "xfs_growfs" for cmd in self.commands))

    def test_kernel_resize_wait_is_bounded_without_service_restart(self):
        now = [0]

        def sleep(delay):
            now[0] += delay

        with patch.object(grow, "run", return_value=str(50 * 1024**3)):
            with self.assertRaisesRegex(grow.UnsafeResize, "kernel_capacity_not_ready"):
                grow.grow(self.config, self.storage, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(now[0], 120)


if __name__ == "__main__":
    unittest.main()
