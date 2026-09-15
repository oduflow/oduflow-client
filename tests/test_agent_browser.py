"""Browser installation contracts and process cleanup without cloud operations."""

import os
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_client_apps import args, high, load_module

cleanup = load_module("browser_cleanup", "agent_browser/files/cleanup.py")


class BrowserStates(unittest.TestCase):
    def test_image_install_is_pinned_and_has_no_client_identity(self):
        data = high("agent_browser/install.sls", {})
        for app in ("agent_browser", "chrome"):
            artifact = args(data[f"agent-browser-package-{app}"])
            self.assertRegex(artifact["source_hash"], r"^sha(256|512)=[a-f0-9]+$")
            self.assertTrue(artifact["source"].startswith("https://"))
        self.assertIn("agent_browser.install", high("client_apps/install.sls", {})["include"])
        self.assertFalse(any("service.running" in state for state in data.values()))

    def test_skills_wait_for_storage_and_are_required_before_paseo(self):
        self.assertIn("agent-browser-invalid-pillar", high("agent_browser/init.sls", {}))
        data = high("agent_browser/init.sls")
        start = args(high("client_apps/start.sls")["client-apps-paseo-running"])
        for agent in ("opencode", "claude", "codex"):
            directory = args(data[f"agent-browser-skills-directory-{agent}"])
            self.assertIn({"cmd": "oduflow-storage-verify"}, directory["require"])
            link = args(data[f"agent-browser-skill-{agent}"])
            self.assertFalse(link["force"])
            self.assertIn({"file": f"agent-browser-skill-{agent}"}, start["require"])


class BrowserCleanup(unittest.TestCase):
    def test_selection_is_by_executable_and_uid_and_includes_children(self):
        items = {
            1: cleanup.Process(1, 1000, 0, 1, "chrome"),
            2: cleanup.Process(2, 1000, 1, 1, "helper"),
            3: cleanup.Process(3, 1000, 2, 1, "helper"),
            4: cleanup.Process(4, 1001, 1, 1, "chrome"),
            5: cleanup.Process(5, 1000, 0, 1, "bash"),
            6: cleanup.Process(6, 0, 0, 1, "agent-browser-linux-x64"),
        }
        with (
            patch.object(cleanup, "process", side_effect=items.get),
            patch.object(Path, "iterdir", return_value=[Path(str(pid)) for pid in items]),
        ):
            self.assertEqual([item.pid for item in cleanup.browser_processes(1000)], [1, 2, 3])

    def test_pid_reuse_cannot_signal_a_replacement_process(self):
        original = cleanup.Process(123, 1000, 1, 10, "chrome")
        replacement = cleanup.Process(123, 1000, 1, 20, "chrome")
        with (
            patch.object(os, "pidfd_open", return_value=99),
            patch.object(os, "close") as close,
            patch.object(cleanup, "process", return_value=replacement),
            patch.object(signal, "pidfd_send_signal") as send,
        ):
            self.assertFalse(cleanup.send(original, signal.SIGKILL))
            send.assert_not_called()
            close.assert_called_once_with(99)

    def test_root_is_refused(self):
        with self.assertRaises(ValueError):
            cleanup.cleanup(0)

    @unittest.skipIf(os.getuid() == 0, "run process checks as an unprivileged user")
    def test_real_processes_terminate_and_sigterm_resistant_process_is_killed(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "chrome"
            shutil.copy2("/bin/sleep", binary)
            for ignore_term in (False, True):
                with self.subTest(ignore_term=ignore_term):
                    child = subprocess.Popen(
                        [str(binary), "60"],
                        preexec_fn=(lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN))
                        if ignore_term
                        else None,
                    )
                    try:
                        item = cleanup.process(child.pid)
                        self.assertIsNotNone(item)
                        # Restrict this unit test to its own child. Full-user cleanup
                        # is tested separately in an isolated disposable container.
                        with patch.object(
                            cleanup,
                            "browser_processes",
                            side_effect=lambda uid: [item] if cleanup.alive(item) else [],
                        ):
                            result = cleanup.cleanup(os.getuid(), grace=0.2)
                        self.assertEqual(result["remaining"], 0)
                        self.assertEqual(result["killed"], int(ignore_term))
                        child.wait(timeout=2)
                    finally:
                        if child.poll() is None:
                            child.kill()
                            child.wait()
