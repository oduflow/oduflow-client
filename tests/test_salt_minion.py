"""No privileged commands or real block devices are used by these tests."""

import argparse
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
UUID = "12345678-1234-1234-1234-123456789abc"
DEVICE = "/dev/disk/by-id/virtio-fixture"
FS_UUID = "8cb51117-a445-41d8-afca-8b56a045a62e"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


storage = load("oduflow_storage", "salt/states/oduflow/files/storage.py")
bootstrap = load("oduflow_bootstrap", "salt/minion/bootstrap.py")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.disk = self.root / "disk"
        self.disk.touch()
        self.link = self.root / "by-id"
        self.link.symlink_to(self.disk)
        self.target = self.root / "mount"
        self.target.mkdir()
        self.receipt = self.root / "storage.json"
        self.swaps = self.root / "swaps"
        self.swaps.write_text("Filename Type Size Used Priority\n")
        self.args = argparse.Namespace(
            device=DEVICE,
            instance_uuid=UUID,
            mount="/srv/oduflow/data",
            timeout=0,
            filesystem_uuid=None,
            verify=False,
            allow_format=False,
        )
        self.disk_info = {
            "name": str(self.disk),
            "type": "disk",
            "maj:min": "252:16",
            "ro": False,
            "rm": False,
        }
        self.mount_info = []
        self.signatures = []
        self.commands = []
        paths = {
            "/etc/oduflow/storage.json": self.receipt,
            "/srv/oduflow/data": self.target,
            "/proc/swaps": self.swaps,
            DEVICE: self.link,
        }
        for patcher in (
            patch.object(
                storage, "Path", side_effect=lambda name: paths.get(str(name), Path(name))
            ),
            patch.object(storage, "wait_device", return_value=str(self.disk)),
            patch.object(storage, "run", side_effect=self.run_command),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_command(self, *args):
        self.commands.append(args)
        if args[0] == "lsblk":
            return json.dumps({"blockdevices": [self.disk_info]})
        if args[0] == "findmnt":
            return json.dumps({"filesystems": self.mount_info})
        if args[0] == "wipefs":
            return json.dumps({"signatures": [{"type": kind} for kind in self.signatures]})
        if args[0] == "blkid":
            return FS_UUID
        if args[0] == "mkfs.xfs":
            self.signatures = ["xfs"]
            return ""
        raise AssertionError(args)

    def assert_refused(self, text):
        with self.assertRaisesRegex(storage.UnsafeStorage, text):
            storage.prepare(self.args)
        self.assertFalse(any(cmd[0] == "mkfs.xfs" for cmd in self.commands))

    def own_xfs(self):
        self.signatures = ["xfs"]
        self.receipt.write_text(
            json.dumps({"instance_uuid": UUID, "device": DEVICE, "filesystem_uuid": FS_UUID})
        )

    def mounted(self, **overrides):
        entry = {
            "target": "/srv/oduflow/data",
            "source": str(self.disk),
            "fstype": "xfs",
            "maj:min": "252:16",
            "options": "rw,relatime,prjquota",
        }
        entry.update(overrides)
        self.mount_info = [entry]

    def test_blank_requires_explicit_authorization(self):
        self.assert_refused("explicit allow_format")

    def test_format_once_records_ownership_and_repeat_is_noop(self):
        self.args.allow_format = True
        self.assertTrue(storage.prepare(self.args))
        self.assertFalse(storage.prepare(self.args))
        self.assertEqual(sum(cmd[0] == "mkfs.xfs" for cmd in self.commands), 1)
        self.assertNotIn("-f", next(cmd for cmd in self.commands if cmd[0] == "mkfs.xfs"))
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o600)

    def test_replaced_blank_volume_is_never_reformatted(self):
        self.own_xfs()
        self.signatures = []
        self.args.allow_format = True
        self.assert_refused("existing ownership")

    def test_partitioned_system_disk_is_refused(self):
        self.disk_info["children"] = [{"name": "/dev/vda1"}]
        self.args.allow_format = True
        self.assert_refused("partitioned")

    def test_mounted_system_disk_is_refused(self):
        self.mounted(target="/")
        self.args.allow_format = True
        self.assert_refused("mounted elsewhere")

    def test_readonly_disk_is_refused(self):
        self.disk_info["ro"] = True
        self.assert_refused("read-only")

    def test_swap_is_refused(self):
        self.swaps.write_text(f"Filename Type Size Used Priority\n{self.disk} partition 1 0 -1\n")
        self.assert_refused("active swap")

    def test_foreign_signatures_are_refused(self):
        for kind in ("ext4", "gpt", "linux_raid_member", "swap"):
            with self.subTest(kind=kind):
                self.signatures = [kind]
                self.args.allow_format = True
                self.assert_refused("non-XFS")

    def test_existing_unknown_xfs_is_refused_even_with_allow_format(self):
        self.signatures = ["xfs"]
        self.args.allow_format = True
        self.assert_refused("must match")

    def test_explicit_existing_uuid_recovers_lost_receipt(self):
        self.signatures = ["xfs"]
        self.args.filesystem_uuid = FS_UUID
        self.assertTrue(storage.prepare(self.args))

    def test_foreign_uuid_is_refused(self):
        self.signatures = ["xfs"]
        self.args.filesystem_uuid = "foreign-uuid"
        self.assert_refused("must match")

    def test_existing_mountpoint_files_are_preserved(self):
        (self.target / "important").write_text("data")
        self.args.allow_format = True
        self.assert_refused("hide existing files")

    def test_nested_mount_is_refused(self):
        self.mounted(target="/srv/oduflow/data/nested", **{"maj:min": "252:17"})
        self.assert_refused("nested mounts")

    def test_owned_mounted_xfs_allows_running_container_overlays_without_format(self):
        self.own_xfs()
        self.mounted()
        self.mount_info.append(
            {
                "target": "/srv/oduflow/data/docker/rootfs/overlayfs/container",
                "source": "overlay",
                "fstype": "overlay",
                "maj:min": "0:43",
                "options": "rw",
            }
        )
        self.assertFalse(storage.prepare(self.args))
        self.args.verify = True
        self.assertFalse(storage.prepare(self.args))
        self.assertFalse(any(command[0] == "mkfs.xfs" for command in self.commands))

    def test_container_overlay_never_authorizes_unknown_or_unmounted_parent(self):
        self.own_xfs()
        self.mounted(
            target="/srv/oduflow/data/docker/rootfs/overlayfs/container",
            fstype="overlay",
            **{"maj:min": "0:43"},
        )
        self.assert_refused("nested mounts")
        self.receipt.unlink()
        self.mounted()
        self.mount_info.append(
            {"target": "/srv/oduflow/data/nested", "fstype": "overlay", "maj:min": "0:43"}
        )
        self.assert_refused("nested mounts")

    def test_wrong_mounted_device_is_refused(self):
        self.mounted(**{"maj:min": "252:17"})
        self.assert_refused("another device")

    def test_service_guard_requires_mounted_quota_filesystem(self):
        self.own_xfs()
        self.args.verify = True
        self.assert_refused("quota mount")
        self.mounted(options="rw,relatime")
        self.assert_refused("quota mount")
        self.mounted()
        self.assertFalse(storage.prepare(self.args))
        self.mounted(options="ro,prjquota")
        self.assert_refused("not writable")

    def test_unstable_device_or_invalid_uuid_is_refused(self):
        self.args.device = "/dev/vdb"
        self.assert_refused("stable")
        self.args.device = DEVICE
        self.args.instance_uuid = UUID.upper()
        self.assert_refused("canonical")

    def test_mount_appearing_during_preparation_is_refused(self):
        self.args.allow_format = True
        first = []
        self.mounted()
        second = self.mount_info
        with patch.object(storage, "mounts", side_effect=[first, second]):
            self.assert_refused("became mounted")

    def test_probe_error_never_formats(self):
        self.args.allow_format = True
        with patch.object(storage, "get_filesystem", side_effect=OSError("probe failed")):
            with self.assertRaises(OSError):
                storage.prepare(self.args)
        self.assertFalse(any(cmd[0] == "mkfs.xfs" for cmd in self.commands))


class BootstrapTests(unittest.TestCase):
    def test_transport_maintenance_matches_bootstrap_without_restarting_a_job(self):
        state = yaml.safe_load((ROOT / "salt/states/client_transport/init.sls").read_text())
        self.assertEqual(set(state), {"client-transport-config"})
        self.assertEqual(set(state["client-transport-config"]), {"file.managed"})
        options = {
            key: value
            for item in state["client-transport-config"]["file.managed"]
            for key, value in item.items()
        }
        self.assertEqual(options["name"], "/etc/salt/minion.d/oduflow-transport.conf")
        self.assertEqual(json.loads(options["contents"]), bootstrap.transport_configuration())
        role = yaml.safe_load((ROOT / "salt/states/roles/client_stack.sls").read_text())
        self.assertIn("client_transport", role["include"])

    def test_transport_is_written_before_the_first_minion_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "public.pem"
            private = root / "private.pem"
            public.write_text("-----BEGIN PUBLIC KEY-----\nfixture\n-----END PUBLIC KEY-----\n")
            private.write_text("fixture private key")
            events = []

            def path(value):
                value = str(value)
                return root / value.lstrip("/") if value.startswith("/etc/") else Path(value)

            def output(command, **kwargs):
                return "salt-minion 3006.27" if command[0] == "salt-minion" else public.read_text()

            argv = [
                "bootstrap.py",
                "--instance-uuid",
                UUID,
                "--master",
                "100.64.0.10",
                "--master-fingerprint",
                ":".join(["ab"] * 32),
                "--private-key",
                str(private),
                "--public-key",
                str(public),
            ]
            with (
                patch("sys.argv", argv),
                patch.object(bootstrap, "Path", side_effect=path),
                patch.object(bootstrap.subprocess, "check_output", side_effect=output),
                patch.object(
                    bootstrap.subprocess,
                    "run",
                    side_effect=lambda command, **kwargs: events.append(("run", command)),
                ),
                patch.object(
                    bootstrap,
                    "write_private",
                    side_effect=lambda target, value: events.append(("write", str(target), value)),
                ),
                patch("builtins.print"),
            ):
                bootstrap.main()
            write = next(
                event
                for event in events
                if event[:2] == ("write", "/etc/salt/minion.d/oduflow-transport.conf")
            )
            start = ("run", ["systemctl", "enable", "--now", "salt-minion"])
            self.assertLess(events.index(write), events.index(start))
            self.assertEqual(json.loads(write[2]), bootstrap.transport_configuration())

    def test_uuid_identity_master_pin_and_no_cached_pillar(self):
        config = bootstrap.configuration(UUID, "100.64.0.10", ":".join(["ab"] * 32))
        self.assertEqual(config["id"], "client-" + UUID)
        self.assertFalse(config["minion_pillar_cache"])
        self.assertFalse(config["open_mode"])
        self.assertEqual(config["hash_type"], "sha256")

    def test_invalid_fingerprint_or_public_master_rejected(self):
        for address, finger in [("1.2.3.4", ":".join(["ab"] * 32)), ("100.64.0.10", "md5")]:
            with self.subTest(address=address), self.assertRaises(ValueError):
                bootstrap.configuration(UUID, address, finger)


class StateRenderingTests(unittest.TestCase):
    def render(self, pillar):
        def get(key, default=None):
            value = pillar
            for part in key.split(":"):
                if not isinstance(value, dict) or part not in value:
                    return default
                value = value[part]
            return value

        env = jinja2.Environment(loader=jinja2.FileSystemLoader(ROOT / "salt/states"))
        # Match Salt's regex_match filter: empty tuple on success, None on failure.
        env.filters["regex_match"] = lambda text, pattern: (
            match.groups() if (match := re.match(pattern, text)) else None
        )
        output = env.get_template("oduflow/storage.sls").render(
            salt={"pillar.get": get}, grains={"id": "client-" + UUID}
        )
        return yaml.safe_load(output)

    def test_absent_pillar_fails_closed_without_disk_actions(self):
        result = self.render({})
        self.assertEqual(list(result), ["oduflow-storage-invalid-pillar"])

    def test_mount_and_service_guards_depend_on_verification(self):
        result = self.render(
            {
                "schema": 1,
                "instance_uuid": UUID,
                "storage": {"device": DEVICE, "mount": "/srv/oduflow/data"},
                "client": {"expires_at": None},
            }
        )
        prepare = result["oduflow-storage-prepare"]["cmd.run"]
        command = next(value["name"] for value in prepare if "name" in value)
        self.assertNotIn("--allow-format", command)
        for service in ("oduflow", "paseo"):
            definition = result[f"oduflow-{service}-storage-guard"]["file.managed"]
            content = next(value["contents"] for value in definition if "contents" in value)
            self.assertIn("BindsTo=srv-oduflow-data.mount", content)
            self.assertIn("--verify --timeout 0", content)
            # Paseo runs unprivileged; only its storage preflight must retain root.
            self.assertIn("ExecStartPre=+/usr/local/libexec/oduflow-storage ", content)
            self.assertIn({"require": [{"cmd": "oduflow-storage-verify"}]}, definition)

    def test_string_true_does_not_authorize_format(self):
        result = self.render(
            {
                "schema": 1,
                "instance_uuid": UUID,
                "storage": {"device": DEVICE, "mount": "/srv/oduflow/data", "allow_format": "true"},
            }
        )
        command = next(
            value["name"]
            for value in result["oduflow-storage-prepare"]["cmd.run"]
            if "name" in value
        )
        self.assertNotIn("--allow-format", command)

    def test_shell_and_systemd_metacharacters_fail_before_commands(self):
        for device in (
            DEVICE + "$(id)",
            DEVICE + "`id`",
            DEVICE + "\n",
            DEVICE + "%n",
            DEVICE + ";id",
        ):
            with self.subTest(device=device):
                result = self.render(
                    {
                        "schema": 1,
                        "instance_uuid": UUID,
                        "storage": {"device": device, "mount": "/srv/oduflow/data"},
                    }
                )
                self.assertEqual(list(result), ["oduflow-storage-invalid-pillar"])
        result = self.render(
            {
                "schema": 1,
                "instance_uuid": UUID,
                "storage": {
                    "device": DEVICE,
                    "mount": "/srv/oduflow/data",
                    "filesystem_uuid": "$(id)",
                },
            }
        )
        self.assertEqual(list(result), ["oduflow-storage-invalid-pillar"])

    def test_real_salt_cmd_state_accepts_rendered_arguments(self):
        try:
            import salt.states.cmd as cmd_state
        except ImportError:
            self.skipTest("Run with installed Salt to exercise the real cmd state")
        result = self.render(
            {
                "schema": 1,
                "instance_uuid": UUID,
                "storage": {"device": DEVICE, "mount": "/srv/oduflow/data"},
            }
        )

        def execute(**kwargs):
            self.assertIs(kwargs["python_shell"], True)
            return {"pid": 1, "retcode": 0, "stdout": '{"changed": false}', "stderr": ""}

        with (
            patch.object(cmd_state, "__opts__", {"test": False}, create=True),
            patch.object(cmd_state, "__grains__", {"shell": "/bin/sh"}, create=True),
            patch.object(cmd_state, "__salt__", {"cmd.run_all": execute}, create=True),
        ):
            for name in ("oduflow-storage-prepare", "oduflow-storage-verify"):
                arguments = {k: v for entry in result[name]["cmd.run"] for k, v in entry.items()}
                ret = cmd_state.run(**arguments)
                self.assertTrue(ret["result"], ret)


if __name__ == "__main__":
    unittest.main()
