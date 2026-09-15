"""Client Git identity is company-scoped and renders safely without shell interpolation."""

import copy
import unittest

from test_client_apps import PILLAR, args, high


class GitIdentity(unittest.TestCase):
    def test_optional_for_old_pillars(self):
        self.assertIn("client-git-identity-not-configured", high("git_identity/init.sls"))

    def test_company_values_are_preserved_for_both_users(self):
        pillar = copy.deepcopy(PILLAR)
        identity = {"name": 'Firma Łódź "Client" $(id)', "email": "company@example.org"}
        pillar["oduflow"]["git_identity"] = identity
        rendered = high("git_identity/init.sls", pillar)
        for user in ("root", "paseo"):
            for field in ("name", "email"):
                state = args(rendered[f"client-git-identity-{user}-{field}"])
                self.assertEqual(state["value"], identity[field])
                self.assertEqual(state["user"], user)
                self.assertTrue(state["global"])

    def test_line_breaks_and_missing_email_are_rejected(self):
        for identity in (
            {"name": "Company", "email": ""},
            {"name": "Company\n[alias]", "email": "a@b.c"},
        ):
            pillar = copy.deepcopy(PILLAR)
            pillar["oduflow"]["git_identity"] = identity
            self.assertIn("client-git-identity-invalid", high("git_identity/init.sls", pillar))
