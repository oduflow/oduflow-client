"""Golden-image build and first-boot contracts; never starts a VM or Docker."""

import unittest
from pathlib import Path

from test_client_apps import args, high

ROOT = Path(__file__).resolve().parents[1]


class GoldenImageTests(unittest.TestCase):
    def test_builder_is_disposable_clean_os_and_pins_tools_without_client_secrets(self):
        template = (ROOT / "packer/client.pkr.hcl").read_text()
        for text in (
            'required_version = "= 1.16.0"',
            'version = "= 2.7.0"',
            # The Paseo source build needs a builder that can compile the
            # monorepo; the published snapshot keeps the 25 GB client contract.
            'plan_id              = "vc2-4c-8gb"',
            "os_id                = 2284",
            "sensitive = true",
        ):
            self.assertIn(text, template)
        self.assertNotIn("snapshot_id", template)
        self.assertNotIn("GITHUB", template)
        self.assertNotIn("LITELLM", template)
        self.assertNotIn("PILLAR", template)

    def test_install_masks_docker_before_packages_and_never_starts_application_role(self):
        install = (ROOT / "packer/scripts/install.sh").read_text()
        self.assertLess(
            install.index("systemctl mask docker.service"),
            install.index("apt-get install -y docker.io"),
        )
        self.assertIn("state.apply roles.client_image,client_apps.install", install)
        self.assertNotIn("state.apply roles.client_stack", install)
        self.assertIn("ConditionPathIsMountPoint=/srv/oduflow/data", install)
        self.assertIn("Container data exists on the boot disk", install)
        self.assertIn("Paseo build tree left in the image", install)
        cleanup = (ROOT / "packer/scripts/clean.sh").read_text()
        self.assertIn("clean-image.py --confirm-disposable-image", cleanup)
        self.assertIn("swapoff /oduflow-image.swap", cleanup)
        self.assertIn("passwd -l root", cleanup)
        self.assertIn("-name authorized_keys", cleanup)

    def test_firstboot_unmasks_only_after_actual_storage_and_config_guards(self):
        states = high("client_apps/docker.sls")
        for service in ("docker", "containerd"):
            unmask = args(states["client-apps-" + service + "-unmask"])
            self.assertIn({"cmd": "oduflow-storage-verify"}, unmask["require"])
            self.assertIn({"cmd": "client-apps-docker-reload"}, unmask["require"])
            running = args(states["client-apps-" + service + "-service"])
            self.assertIn({"service": "client-apps-" + service + "-unmask"}, running["require"])
        socket = args(states["client-apps-docker-socket-unmask"])
        self.assertIn({"service": "client-apps-docker-service"}, socket["require_in"])


if __name__ == "__main__":
    unittest.main()
