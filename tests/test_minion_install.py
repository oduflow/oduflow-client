"""Run the installer in a redirected filesystem with package/service commands mocked."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOCK = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["MOCK_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\n")
if name == "id": print("0")
elif name == "uname": print("x86_64")
elif name == "dpkg-query":
    package = args[-1]
    if not os.environ.get("MOCK_PREINSTALLED") or package == os.environ.get("MOCK_MISSING_PACKAGE"):
        sys.exit(1)
    version = "1.102.4" if package == "tailscale" else "3006.27"
    if package == os.environ.get("MOCK_WRONG_PACKAGE"):
        version = "0.0.1"
    print("installed " + version if "${Version}" in args[1] else "installed")
elif name == "systemctl" and args[0] == "is-active": sys.exit(3)
elif name == "curl": pathlib.Path(args[args.index("-o") + 1]).write_text("fixture key")
elif name == "gpg": pathlib.Path(args[args.index("-o") + 1]).write_text("fixture keyring")
elif name == "sha256sum":
    sys.stdin.read()
    sys.exit(int(os.environ.get("MOCK_BAD_HASH", "0")))
elif name == "salt-minion": print(os.environ.get("MOCK_SALT_VERSION", "salt-minion 3006.27"))
elif name == "tailscale": print(os.environ.get("MOCK_TAILSCALE_VERSION", "1.102.4"))
"""


class MinionInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="minion-install-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.log = self.root / "commands.jsonl"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in (
            "id",
            "uname",
            "dpkg-query",
            "systemctl",
            "apt-get",
            "curl",
            "gpg",
            "sha256sum",
            "salt-minion",
            "tailscale",
        ):
            path = self.bin / name
            path.write_text(MOCK)
            path.chmod(0o755)
        for name in ("etc/apt/sources.list.d", "run/systemd/system"):
            (self.root / name).mkdir(parents=True)
        self.os_release("24.04", "noble")
        script = (ROOT / "salt/minion/install.sh").read_text()
        for prefix in ("/etc/", "/var/lib/", "/usr/share/keyrings", "/run/systemd/system"):
            script = script.replace(prefix, str(self.root) + prefix)
        self.script = self.root / "install.sh"
        self.script.write_text(script)

    def os_release(self, version, codename):
        (self.root / "etc/os-release").write_text(
            f'ID=ubuntu\nVERSION_ID="{version}"\nVERSION_CODENAME={codename}\n'
        )

    def run_installer(self, **extra):
        return subprocess.run(
            ["bash", str(self.script)],
            capture_output=True,
            text=True,
            timeout=20,
            env={
                **os.environ,
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "MOCK_LOG": str(self.log),
                **extra,
            },
        )

    def commands(self):
        import json

        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_supported_releases_install_pins_after_masking(self):
        for version, codename in (("24.04", "noble"), ("26.04", "resolute")):
            with self.subTest(codename=codename):
                self.log.write_text("")
                self.os_release(version, codename)
                result = self.run_installer()
                self.assertEqual(result.returncode, 0, result.stderr)
                commands = self.commands()
                mask = commands.index(["systemctl", "mask", "salt-minion.service"])
                first_apt = next(i for i, cmd in enumerate(commands) if cmd[0] == "apt-get")
                self.assertLess(mask, first_apt)
                self.assertIn(
                    [
                        "apt-get",
                        "install",
                        "-y",
                        "salt-common=3006.27",
                        "salt-minion=3006.27",
                        "tailscale=1.102.4",
                    ],
                    commands,
                )
                self.assertNotIn(["tailscale", "up"], commands)
                self.assertIn(
                    codename, (self.root / "etc/apt/sources.list.d/tailscale.list").read_text()
                )

    def test_existing_identity_fails_before_service_or_package_changes(self):
        identity = self.root / "etc/oduflow/instance.json"
        identity.parent.mkdir()
        identity.write_text('{"instance_uuid":"existing"}')
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertTrue(all(cmd[0] in ("id", "uname") for cmd in self.commands()))
        self.assertEqual(identity.read_text(), '{"instance_uuid":"existing"}')

    def test_existing_pki_fails_before_mutation(self):
        key = self.root / "etc/salt/pki/minion/minion.pem"
        key.parent.mkdir(parents=True)
        key.write_text("existing key")
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertTrue(all(cmd[0] in ("id", "uname") for cmd in self.commands()))

    def test_unsupported_os_is_rejected_before_mutation(self):
        self.os_release("22.04", "jammy")
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertTrue(all(cmd[0] in ("id", "uname") for cmd in self.commands()))

    def test_existing_vpn_identity_is_preserved_and_refused_before_mutation(self):
        state = self.root / "var/lib/tailscale/tailscaled.state"
        state.parent.mkdir(parents=True)
        state.write_text('{"identity":"existing-node"}')
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertTrue(all(cmd[0] in ("id", "uname") for cmd in self.commands()))
        self.assertEqual(state.read_text(), '{"identity":"existing-node"}')

    def test_vpn_state_symlink_is_refused_even_when_target_is_missing(self):
        state = self.root / "var/lib/tailscale/tailscaled.state"
        state.parent.mkdir(parents=True)
        state.symlink_to(self.root / "missing-state")
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertTrue(all(cmd[0] in ("id", "uname") for cmd in self.commands()))
        self.assertTrue(state.is_symlink())

    def test_clean_preinstalled_host_skips_all_package_and_network_commands(self):
        result = self.run_installer(
            MOCK_PREINSTALLED="1", MOCK_SALT_VERSION="salt-minion 3006.27 (Sulfur)"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        self.assertFalse(any(cmd[0] in ("apt-get", "curl", "gpg") for cmd in commands))
        self.assertIn(["salt-minion", "--version"], commands)
        self.assertIn(["tailscale", "version"], commands)
        self.assertIn(["systemctl", "mask", "salt-minion.service"], commands)
        self.assertIn(["systemctl", "stop", "salt-minion.service"], commands)
        self.assertFalse(any(cmd[:2] == ["systemctl", "start"] for cmd in commands))

    def test_preinstalled_host_missing_prerequisite_runs_normal_install(self):
        result = self.run_installer(MOCK_PREINSTALLED="1", MOCK_MISSING_PACKAGE="xfsprogs")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["apt-get", "update"], self.commands())

    def test_preinstalled_host_rejects_wrong_binary_before_service_changes(self):
        for variable, version in (
            ("MOCK_SALT_VERSION", "salt-minion 3006.2"),
            ("MOCK_TAILSCALE_VERSION", "1.102.40"),
        ):
            with self.subTest(variable=variable):
                self.log.write_text("")
                result = self.run_installer(MOCK_PREINSTALLED="1", **{variable: version})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(cmd[0] in ("apt-get", "curl") for cmd in self.commands()))
                self.assertFalse(any(cmd[:2] == ["systemctl", "mask"] for cmd in self.commands()))

    def test_preinstalled_wrong_package_version_fails_before_mutation(self):
        result = self.run_installer(MOCK_PREINSTALLED="1", MOCK_WRONG_PACKAGE="salt-common")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(cmd[:2] == ["systemctl", "mask"] for cmd in self.commands()))
        self.assertFalse(any(cmd[0] == "apt-get" for cmd in self.commands()))

    def test_preinstalled_host_still_refuses_existing_identity(self):
        identity = self.root / "etc/oduflow/instance.json"
        identity.parent.mkdir()
        identity.write_text('{"instance_uuid":"existing"}')
        self.assertNotEqual(self.run_installer(MOCK_PREINSTALLED="1").returncode, 0)
        self.assertTrue(all(cmd[0] in ("id", "uname") for cmd in self.commands()))

    def test_bad_key_hash_never_installs_salt(self):
        self.assertNotEqual(self.run_installer(MOCK_BAD_HASH="1").returncode, 0)
        self.assertFalse(any("salt-minion=3006.27" in cmd for cmd in self.commands()))
        self.assertFalse((self.root / "etc/apt/sources.list.d/salt.sources").exists())


if __name__ == "__main__":
    unittest.main()
