"""Public verification uses mocked HTTPS and never writes production state."""

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "verify_production", Path(__file__).resolve().parents[1] / "scripts/verify-client-production.py"
)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "name": "main",
            "domain": "client.example.org",
            "team_hostname": "oduflow.client.example.org",
            "admin_password_file": "/admin",
            "ui_password_file": "/ui",
        }
        self.info = {
            "db_name": "production_db",
            "container_status": "running",
            "deploy_in_progress": False,
        }
        self.receipt = {"status": "hardened", "fingerprint": "digest", "container_id": "a" * 64}
        self.helper = Mock()
        self.helper.validate.side_effect = lambda value: value
        self.helper.fingerprint.return_value = "digest"
        self.helper.private_json.side_effect = lambda path: (
            self.config if path == mod.CONFIG else self.receipt
        )
        self.helper.LocalAPI.return_value.info.return_value = self.info
        self.helper.ODUFLOW_CONFIG = "/toml"
        values = {
            "/admin": "secret-admin-password",
            "/ui": "secret-ui-password",
            "/toml": '[route.paseo]\nhost="ide.client.example.org"\n',
            mod.PASEO_ENV: (
                "PASEO_PASSWORD=secret-paseo-password\nODUFLOW_MCP_TOKEN=secret-oduflow-mcp-token\n"
            ),
        }
        self.helper.private_read.side_effect = lambda path: values[path]
        self.factory = Mock(side_effect=lambda: Mock())
        self.responses = [
            (
                200,
                {"result": {"uid": 2, "server_version": "19.0", "server_version_info": [19, 0, 0]}},
            ),
            (200, {"jsonrpc": "2.0", "id": 1}),
            (200, {"error": {"data": {"name": "odoo.http.SessionExpiredException"}}}),
            (401, {"error": "secret-response-body"}),
            (200, {"ok": True}),
            (401, {"error": "secret-response-body"}),
            (200, {"status": "ok"}),
        ]

    def run_check(self):
        with patch.object(mod, "request", side_effect=self.responses) as request:
            result = mod.verify(self.helper, factory=self.factory)
        return result, request

    def test_hardened_identity_login_version_and_both_protected_apis(self):
        result, request = self.run_check()
        self.assertTrue(result["overall"])
        self.helper.verify_container.assert_called_once_with(self.info, self.config, "a" * 64)
        self.helper.execute.assert_not_called()
        self.helper.harden.assert_not_called()
        self.helper.save_state.assert_not_called()
        self.assertTrue(all(call.args[2].startswith("https://") for call in request.call_args_list))
        self.assertEqual(request.call_args_list[0].kwargs["json"]["params"]["db"], "production_db")
        self.assertTrue(request.call_args_list[1].args[2].endswith("/web/session/destroy"))
        self.assertNotIn("secret-", json.dumps(result))
        self.assertEqual(self.factory.call_count, 3)
        self.assertTrue(
            any(
                call.args[2].startswith("https://ide.client.example.org/")
                for call in request.call_args_list
            )
        )

    def test_replaced_container_stops_before_any_public_credentials(self):
        self.helper.verify_container.side_effect = ValueError("secret-container-details")
        result, request = self.run_check()
        self.assertFalse(result["overall"])
        self.assertFalse(result["checks"]["local_hardened_identity"])
        request.assert_not_called()
        self.assertNotIn("secret", json.dumps(result))

    def test_unhardened_receipt_does_not_call_api_or_modify_state(self):
        self.receipt["status"] = "claimed"
        result, request = self.run_check()
        self.assertFalse(result["overall"])
        self.helper.LocalAPI.assert_not_called()
        request.assert_not_called()

    def test_failed_odoo_login_still_destroys_session(self):
        self.responses[0] = (200, {"error": {"message": "secret-login-error"}})
        result, request = self.run_check()
        self.assertFalse(result["overall"])
        self.assertFalse(result["checks"]["odoo_admin_login"])
        self.assertTrue(result["checks"]["odoo_logout"])
        self.assertTrue(request.call_args_list[1].args[2].endswith("/destroy"))
        self.assertNotIn("secret", json.dumps(result))

    def test_wrong_major_version_fails(self):
        self.responses[0] = (200, {"result": {"uid": 2, "server_version": "18.0"}})
        result, _ = self.run_check()
        self.assertFalse(result["overall"])
        self.assertFalse(result["checks"]["odoo_version_19"])

    def test_open_anonymous_api_fails_even_with_authenticated_success(self):
        self.responses[3] = (200, {"ok": True})
        result, _ = self.run_check()
        self.assertFalse(result["overall"])
        self.assertFalse(result["checks"]["oduflow_anonymous_denied"])

    def test_logout_failure_fails_overall(self):
        self.responses[1] = (500, {"error": "secret"})
        result, _ = self.run_check()
        self.assertFalse(result["overall"])
        self.assertFalse(result["checks"]["odoo_logout"])

    def test_logout_success_requires_session_to_be_expired(self):
        self.responses[2] = (200, {"jsonrpc": "2.0", "id": 1})
        result, _ = self.run_check()
        self.assertFalse(result["overall"])
        self.assertFalse(result["checks"]["odoo_logout"])

    def test_empty_204_logout_still_requires_expired_session(self):
        self.responses[1] = (204, None)
        result, request = self.run_check()
        self.assertTrue(result["overall"])
        self.assertTrue(request.call_args_list[2].args[2].endswith("/check"))

    def test_unrelated_session_check_error_does_not_prove_logout(self):
        self.responses[2] = (200, {"error": {"data": {"name": "odoo.exceptions.UserError"}}})
        result, _ = self.run_check()
        self.assertFalse(result["overall"])
        self.assertFalse(result["checks"]["odoo_logout"])

    def test_https_request_requires_cert_verification_and_no_redirects(self):
        client = Mock()
        response = client.request.return_value
        response.status_code = 302
        response.iter_content.return_value = [b'{"secret": "body"}']
        code, _ = mod.request(client, "GET", "https://fixture.example.org/api/status")
        self.assertEqual(code, 302)
        self.assertTrue(client.request.call_args.kwargs["verify"])
        self.assertFalse(client.request.call_args.kwargs["allow_redirects"])
        response.close.assert_called_once()

    def test_response_size_is_bounded(self):
        client = Mock()
        client.request.return_value.iter_content.return_value = [b"x" * (mod.LIMIT + 1)]
        with self.assertRaises(mod.CheckFailed):
            mod.request(client, "GET", "https://fixture.example.org/api/status")
        client.request.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
