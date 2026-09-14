"""Application readiness probes use real local HTTP/auth and bounded retry tests."""

import base64
import contextlib
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from test_client_apps import args, high, load_module

health = load_module("client_health", "client_apps/files/health.py")
AUTH = {"oduflow": "Basic fixture-only", "paseo": "Bearer fixture-only"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.calls.append((self.path, self.headers.get("Authorization")))
        if self.path in ("/healthz", "/api/health"):
            status = self.server.health_status
        elif self.path in ("/api/productions", "/api/status"):
            service = "oduflow" if self.path == "/api/productions" else "paseo"
            status = 200 if self.headers.get("Authorization") == AUTH[service] else 401
            if self.server.auth_disabled:
                status = 200
        else:
            status = 404
        self.send_response(status)
        self.end_headers()


class ClientHealthTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.calls = []
        self.server.health_status = 200
        self.server.auth_disabled = False
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        targets = patch.dict(health.TARGETS, dict.fromkeys(AUTH, self.server.server_address))
        targets.start()
        self.addCleanup(targets.stop)

    def test_real_http_checks_require_both_auth_denial_and_success(self):
        self.assertEqual(health.wait_ready(AUTH, timeout=2), (True, []))
        self.assertEqual(len(self.server.calls), 6)
        self.assertIn(("/api/productions", AUTH["oduflow"]), self.server.calls)
        self.assertIn(("/api/status", AUTH["paseo"]), self.server.calls)
        self.assertIn(("/api/status", None), self.server.calls)

    def test_running_server_with_broken_health_or_disabled_auth_is_not_ready(self):
        self.server.health_status = 503
        ready, failures = health.wait_ready(AUTH, timeout=0.05)
        self.assertFalse(ready)
        self.assertIn("oduflow_health", failures)
        self.server.health_status = 200
        self.server.auth_disabled = True
        ready, failures = health.wait_ready(AUTH, timeout=0.05)
        self.assertFalse(ready)
        self.assertIn("paseo_auth_required", failures)

    def test_startup_retry_and_total_deadline_are_bounded(self):
        now = [0]
        calls = []

        def fake_request(service, path, authorization, timeout):
            calls.append(timeout)
            if now[0] == 0:
                raise TimeoutError("sensitive response must never escape")
            return 200 if authorization or path in ("/healthz", "/api/health") else 401

        with patch.object(health, "request_status", side_effect=fake_request):
            self.assertEqual(
                health.wait_ready(
                    AUTH,
                    timeout=5,
                    clock=lambda: now[0],
                    sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                ),
                (True, []),
            )
        self.assertLessEqual(max(calls), 3)
        now[0] = 0
        with patch.object(health, "request_status", side_effect=TimeoutError("secret")):
            ready, _ = health.wait_ready(
                AUTH,
                timeout=10000,
                clock=lambda: now[0],
                sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
            )
        self.assertFalse(ready)
        self.assertEqual(now[0], 240)

    def test_credential_parsing_and_private_file_permissions(self):
        password = "fixture-password-only"
        with patch.object(
            health,
            "read_private",
            side_effect=[f'[team.1]\nui_password="{password}"', f"PASEO_PASSWORD={password}\n"],
        ):
            credentials = health.credentials()
        self.assertEqual(
            base64.b64decode(credentials["oduflow"].split()[1]), ("admin:" + password).encode()
        )
        self.assertEqual(credentials["paseo"], "Bearer " + password)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config"
            path.write_text("fixture only")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                health.read_private(path)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                health.read_private(link)

    def test_output_is_stateful_and_never_contains_raw_failure(self):
        output = io.StringIO()
        with (
            patch.object(health, "credentials", side_effect=ValueError("sk-secret-value")),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(health.main(), 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result["changed"])
        self.assertNotIn("sk-secret-value", output.getvalue())
        self.assertIn("private_configuration_invalid", result["comment"])

    def test_role_completion_requires_health_after_services_without_cycle(self):
        role = high("roles/client_stack.sls")
        self.assertEqual(
            role["include"],
            [
                "client_receipts",
                "client_transport",
                "client_apps.health",
                "client_ssh",
                "github_downloads",
            ],
        )
        data = high("client_apps/health.sls")
        self.assertEqual(data["include"], ["client_apps.start", "paseo.project"])
        probe = args(data["client-apps-health"])
        self.assertTrue(probe["stateful"])
        self.assertEqual(probe["timeout"], 250)
        self.assertIn({"service": "client-apps-oduflow-running"}, probe["require"])
        self.assertIn({"service": "client-apps-paseo-running"}, probe["require"])
        self.assertNotIn("client_apps.health", high("client_apps/start.sls")["include"])


if __name__ == "__main__":
    unittest.main()
