"""Minion-side receipts bind identity before state work and suppress raw returns."""

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "minion_job", ROOT / "salt/minion/extmods/modules/oduflow_job.py"
)
job = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(job)
REAL_SAFETY = job._execution_safety
REAL_SUPPORTED = job._supported_runtime
MINION = "client-683a74a5-9ef6-4512-995a-9d6338c008bc"
REQUEST = "41c5b701-3777-42d0-b8d2-19cdff3f8b6c"
SECOND = "683a74a5-9ef6-4512-995a-9d6338c008bc"
JID = "20260912010000123456"


class MinionReceiptTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "receipts"
        # Simulate root's owner checks without requiring a privileged test process.
        for patcher in (
            patch.object(job, "_ROOT", self.root),
            patch.object(job, "_OWNER_UID", os.getuid()),
            patch.object(job.os, "geteuid", return_value=0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        job.__opts__ = {"id": MINION, "cache_jobs": False, "minion_pillar_cache": False}
        for patcher in (
            patch.object(job, "_execution_safety", side_effect=lambda: nullcontext()),
            patch.object(job, "_supported_runtime", return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.apply = Mock(
            return_value={
                "sensitive-state-name": {
                    "result": True,
                    "comment": "SECRET",
                    "changes": {"password": "SECRET"},
                }
            }
        )
        self.high = Mock(return_value={"custom-secret": {"result": True}})
        job.__salt__ = {"state.apply": self.apply, "state.high": self.high}
        self.receipt = self.root / ("request-" + REQUEST + ".json")
        self.index = self.root / ("jid-" + JID + ".json")

    def run_job(self, request=REQUEST, profile="configure", jid=JID, **kwargs):
        return job.run(request, profile, __pub_jid=jid, **kwargs)

    def test_release_is_bound_before_execution_and_cannot_be_changed_on_replay(self):
        commit = "a" * 40
        job.__salt__["oduflow_release.options"] = lambda *args: nullcontext({})
        result = self.run_job(client_revision=commit)
        self.assertEqual(result["protocol"], 2)
        self.assertEqual(result["client_revision"], commit)
        self.assertEqual(job.version()["verified"], commit)
        self.assertEqual(self.run_job(client_revision=commit), result)
        with self.assertRaisesRegex(RuntimeError, "binding_conflict"):
            self.run_job(client_revision="b" * 40)
        self.apply.assert_called_once()
        self.assertEqual(job.status(JID, REQUEST, "configure", "", commit), result)
        self.apply.return_value = {"failed": {"result": False}}
        self.run_job(request=SECOND, jid="20260912010000123457", client_revision="b" * 40)
        self.assertEqual(job.version()["desired"], "b" * 40)
        self.assertEqual(job.version()["verified"], commit)

    def test_claims_are_private_and_fsynced_before_execution_and_result_before_return(self):
        original = job.os.fsync
        synced = []

        def record_fsync(fd):
            synced.append(os.fstat(fd).st_ino)
            return original(fd)

        def execute(*args, **kwargs):
            self.assertEqual(json.loads(self.receipt.read_text())["status"], "running")
            self.assertEqual(json.loads(self.index.read_text())["jid"], JID)
            self.assertIn(self.index.stat().st_ino, synced)
            self.assertIn(self.receipt.stat().st_ino, synced)
            self.assertIn(self.root.stat().st_ino, synced)
            return self.apply.return_value

        self.apply.side_effect = execute
        with patch.object(job.os, "fsync", side_effect=record_fsync):
            result = self.run_job()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(json.loads(self.receipt.read_text()), result)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o600)
        for path in self.root.iterdir():
            self.assertNotIn("SECRET", path.read_text())
            self.assertNotIn("sensitive-state-name", path.read_text())
        self.assertNotIn("SECRET", str(result))

    def test_each_profile_executes_only_its_fixed_role_with_events_and_queue_disabled(self):
        for index, (profile, role) in enumerate(job._PROFILES.items()):
            if profile == "custom":
                continue
            with self.subTest(profile=profile):
                request = f"41c5b701-3777-42d0-b8d2-{index:012d}"
                jid = f"20260912010000{index:06d}"
                self.run_job(request, profile, jid)
                self.apply.assert_called_with(
                    role,
                    test=False,
                    queue=False,
                    concurrent=False,
                    state_events=False,
                    __pub_jid=jid,
                )

    def test_duplicate_same_publication_returns_receipt_without_execution(self):
        first = self.run_job()
        self.assertEqual(self.run_job(), first)
        self.assertEqual(job.status(JID, REQUEST, "configure"), first)
        self.apply.assert_called_once()

    def test_jid_and_request_are_both_permanently_bound(self):
        self.run_job()
        for request, profile, jid in (
            (SECOND, "configure", JID),
            (REQUEST, "production", JID),
            (REQUEST, "configure", "20260912010000123457"),
        ):
            with (
                self.subTest(request=request, profile=profile, jid=jid),
                self.assertRaisesRegex(job._JobError, "binding_conflict"),
            ):
                self.run_job(request, profile, jid)
        self.apply.assert_called_once()

    def test_status_is_running_only_while_exclusive_lock_is_live(self):
        def execute(*args, **kwargs):
            self.assertEqual(job.status(JID, REQUEST, "configure")["status"], "running")
            self.assertEqual(self.run_job()["status"], "running")
            return {"one": {"result": True}}

        self.apply.side_effect = execute
        self.assertEqual(self.run_job()["status"], "succeeded")
        self.apply.assert_called_once()

    def test_other_running_request_does_not_make_abandoned_claim_look_live(self):
        self.run_job()
        record = json.loads(self.receipt.read_text())
        record.update(status="running", passed=0, total=0)
        self.receipt.write_text(json.dumps(record))

        def execute(*args, **kwargs):
            self.assertEqual(job.status(JID, REQUEST, "configure")["status"], "unknown")
            return {"one": {"result": True}}

        self.apply.side_effect = execute
        result = self.run_job(SECOND, "production", "20260912010000123457")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.apply.call_count, 2)

    def test_partial_index_claim_never_executes_after_interrupted_preparation(self):
        original = job._write

        def interrupt(directory, name, data):
            if name.startswith("request-"):
                raise OSError("SECRET")
            return original(directory, name, data)

        with (
            patch.object(job, "_write", side_effect=interrupt),
            self.assertRaisesRegex(job._JobError, "receipt_unavailable"),
        ):
            self.run_job()
        self.assertEqual(self.run_job()["status"], "unknown")
        self.assertEqual(job.status(JID, REQUEST, "configure")["status"], "unknown")
        self.apply.assert_not_called()

    def test_abandoned_running_claim_never_reexecutes(self):
        self.run_job()
        record = json.loads(self.receipt.read_text())
        record.update(status="running", passed=0, total=0)
        self.receipt.write_text(json.dumps(record))
        self.assertEqual(job.status(JID, REQUEST, "configure")["status"], "unknown")
        self.assertEqual(self.run_job()["status"], "unknown")
        self.apply.assert_called_once()

    def test_terminal_write_failure_leaves_durable_claim_unknown(self):
        original = job._write

        def fail_terminal(directory, name, data):
            if data.get("status") == "succeeded":
                raise OSError("SECRET")
            return original(directory, name, data)

        with (
            patch.object(job, "_write", side_effect=fail_terminal),
            self.assertRaisesRegex(job._JobError, "receipt_unavailable"),
        ):
            self.run_job()
        self.assertEqual(self.run_job()["status"], "unknown")
        self.apply.assert_called_once()

    def test_failed_states_only_return_counts(self):
        self.apply.return_value = {
            "secret-one": {"result": True},
            "secret-two": {"result": False, "comment": "SECRET"},
        }
        result = self.run_job()
        self.assertEqual((result["status"], result["passed"], result["total"]), ("failed", 1, 2))
        self.assertNotIn("SECRET", self.receipt.read_text())

    def test_exception_and_unstructured_results_are_unknown_without_raw_errors(self):
        self.apply.side_effect = RuntimeError("SECRET")
        self.assertEqual(self.run_job()["status"], "unknown")
        self.assertNotIn("SECRET", self.receipt.read_text())
        for value in ({}, ["SECRET"], {"test": {"result": None}}, {"test": {"result": 1}}):
            self.assertEqual(
                job._reduce(job._binding(JID, REQUEST, "configure"), value)["status"], "unknown"
            )

    def test_custom_payload_digest_is_bound_and_raw_content_is_not_saved(self):
        payload = json.dumps(
            {"private-name": {"test.nop": [{"name": "SECRET"}]}},
            sort_keys=True,
            separators=(",", ":"),
        )
        result = self.run_job(profile="custom", state_data_json=payload)
        self.high.assert_called_once_with(
            {"private-name": {"test": ["nop", {"name": "SECRET"}]}},
            test=False,
            queue=False,
            concurrent=False,
            state_events=False,
            __pub_jid=JID,
        )
        self.assertEqual(job.status(JID, REQUEST, "custom", result["state_digest"]), result)
        with self.assertRaisesRegex(job._JobError, "binding_conflict"):
            job.status(JID, REQUEST, "custom", "0" * 64)
        with self.assertRaisesRegex(job._JobError, "binding_conflict"):
            self.run_job(profile="custom", state_data_json=payload.replace("SECRET", "DIFFERENT"))
        self.assertNotIn("SECRET", self.receipt.read_text())
        self.assertNotIn("private-name", self.receipt.read_text())

    def test_custom_template_noncanonical_or_extended_high_data_is_refused(self):
        for payload in (
            '{"x": {"test.nop": []}}',
            '{"include":["anything"]}',
            '{"x":{"test.nop":[{"name":"{{SECRET}}"}]}}',
            '{"x":{"test.nop":["raw"]}}',
            "{}",
        ):
            with self.subTest(payload=payload), self.assertRaises(job._JobError):
                self.run_job(profile="custom", state_data_json=payload)
        self.high.assert_not_called()

    def test_arbitrary_execution_options_and_invalid_identity_refused(self):
        for kw in (
            {"test": True},
            {"queue": True},
            {"concurrent": True},
            {"pillar": {"secret": "SECRET"}},
            {"saltenv": "other"},
        ):
            with self.subTest(kwargs=kw), self.assertRaises(job._JobError):
                self.run_job(**kw)
        for jid in (None, "../bad", "2026", True):
            with self.assertRaises(job._JobError):
                self.run_job(jid=jid)
        with self.assertRaises(job._JobError):
            self.run_job(profile="cmd.run")
        self.apply.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_custom_parallel_options_are_refused_before_claim(self):
        for value in (True, False):
            payload = json.dumps(
                {"private-name": {"test.nop": [{"parallel": value}]}},
                sort_keys=True,
                separators=(",", ":"),
            )
            with self.subTest(value=value), self.assertRaises(job._JobError):
                self.run_job(profile="custom", state_data_json=payload)
        self.high.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_capability_and_run_share_preclaim_runtime_checks(self):
        for option in ("test", "cache_jobs", "minion_pillar_cache"):
            with patch.dict(job.__opts__, {option: True}):
                for method in (job.capability, self.run_job):
                    with (
                        self.subTest(option=option),
                        self.assertRaisesRegex(job._JobError, "unsafe_options"),
                    ):
                        method()
        with patch.object(
            job, "_supported_runtime", side_effect=job._JobError("runtime_unsupported")
        ):
            for method in (job.capability, self.run_job):
                with self.assertRaisesRegex(job._JobError, "runtime_unsupported"):
                    method()
        self.apply.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_real_publication_metadata_is_accepted(self):
        self.assertEqual(
            self.run_job(
                __pub_user="control",
                __pub_fun="oduflow_job.run",
                __pub_arg=[],
                __pub_tgt=MINION,
                __pub_tgt_type="list",
                __pub_master_id="master",
                __pub_metadata={},
                __pub_ret_config=None,
            )["status"],
            "succeeded",
        )

    def test_nonroot_and_unsafe_cache_options_refuse_before_execution(self):
        with patch.object(job.os, "geteuid", return_value=123), self.assertRaises(job._JobError):
            self.run_job()
        for option in ("test", "cache_jobs", "minion_pillar_cache"):
            with patch.dict(job.__opts__, {option: True}), self.assertRaises(job._JobError):
                self.run_job()
        self.apply.assert_not_called()

    def test_foreign_modes_links_and_corruption_are_rejected(self):
        self.run_job()
        original = self.receipt.read_text()
        for mode in (0o644, 0o666):
            self.receipt.chmod(mode)
            with self.assertRaisesRegex(job._JobError, "storage_invalid"):
                job.status(JID, REQUEST, "configure")
        self.receipt.chmod(0o600)
        alias = self.root / "foreign"
        os.link(self.receipt, alias)
        with self.assertRaisesRegex(job._JobError, "storage_invalid"):
            self.run_job()
        alias.unlink()
        self.receipt.unlink()
        self.receipt.symlink_to(self.index)
        with self.assertRaises(job._JobError):
            self.run_job()
        self.receipt.unlink()
        self.receipt.write_text("not-json-SECRET")
        self.receipt.chmod(0o600)
        with self.assertRaisesRegex(job._JobError, "receipt_unavailable"):
            job.status(JID, REQUEST, "configure")
        self.receipt.write_text(original)
        self.root.chmod(0o755)
        with self.assertRaisesRegex(job._JobError, "storage_invalid"):
            self.run_job()
        self.root.chmod(0o700)
        self.apply.assert_called_once()

    def test_pinned_salt_cache_and_event_shims_are_bounded_and_restored(self):
        salt = types.ModuleType("salt")
        salt.state = types.ModuleType("salt.state")
        salt.utils = types.ModuleType("salt.utils")
        salt.utils.files = types.ModuleType("salt.utils.files")
        salt.state.State = type("State", (), {"event": Mock(), "call_parallel": Mock()})
        original_open = Mock(side_effect=open)
        original_event = salt.state.State.event
        original_parallel = salt.state.State.call_parallel
        salt.utils.files.fopen = original_open
        modules = {
            "salt": salt,
            "salt.state": salt.state,
            "salt.utils": salt.utils,
            "salt.utils.files": salt.utils.files,
        }
        cache = self.root.parent / "cache"
        cache.mkdir()
        job.__opts__["cachedir"] = str(cache)
        with patch.dict(sys.modules, modules):
            with REAL_SAFETY():
                for name in ("sls.p", "highstate.cache.p", "custom.cache.p"):
                    with salt.utils.files.fopen(cache / name, "w+b") as stream:
                        stream.write(b"SECRET-state-output")
                    self.assertFalse((cache / name).exists())
                with salt.utils.files.fopen(cache / "normal-file", "w+b") as stream:
                    stream.write(b"ordinary")
                salt.state.State.event(None, {"SECRET": True}, 1, fire_event=True)
                original_event.assert_not_called()
                with self.assertRaisesRegex(job._JobError, "parallel_unsupported"):
                    salt.state.State.call_parallel(None, {"parallel": True})
                original_parallel.assert_not_called()
                with self.assertRaisesRegex(job._JobError, "cache_read_refused"):
                    salt.utils.files.fopen(cache / "sls.p", "rb")
            self.assertIs(salt.utils.files.fopen, original_open)
            self.assertIs(salt.state.State.event, original_event)
            self.assertIs(salt.state.State.call_parallel, original_parallel)
            with self.assertRaises(RuntimeError), REAL_SAFETY():
                raise RuntimeError("simulated state failure")
            self.assertIs(salt.utils.files.fopen, original_open)
            self.assertIs(salt.state.State.event, original_event)
            self.assertIs(salt.state.State.call_parallel, original_parallel)
        self.assertEqual((cache / "normal-file").read_bytes(), b"ordinary")
        original_open.assert_called_once()

    def test_unknown_salt_version_and_threaded_minions_are_rejected(self):
        salt = types.ModuleType("salt")
        salt.version = types.ModuleType("salt.version")
        salt.version.__version__ = "3006.27"
        with patch.dict(sys.modules, {"salt": salt, "salt.version": salt.version}):
            REAL_SUPPORTED()
            salt.version.__version__ = "3007.0"
            with self.assertRaisesRegex(job._JobError, "runtime_unsupported"):
                REAL_SUPPORTED()
            salt.version.__version__ = "3006.27"
            with (
                patch.dict(job.__opts__, {"multiprocessing": False}),
                self.assertRaisesRegex(job._JobError, "runtime_unsupported"),
            ):
                REAL_SUPPORTED()

    def test_missing_receipt_is_not_started_but_does_not_prove_a_safe_master_retry(self):
        result = job.status(JID, REQUEST, "configure")
        self.assertEqual(result["status"], "not_started")
        self.assertFalse(self.root.exists())
        self.assertEqual(job.capability()["protocol"], 1)


class PinnedSaltIntegrationTests(unittest.TestCase):
    def test_real_publisher_and_state_functions_suppress_raw_cache_and_events(self):
        try:
            import salt.config
            import salt.loader
            import salt.state
            import salt.version

            import salt.minion
        except ImportError:
            self.skipTest("Salt 3006.27 dependencies are required for integration")
        if salt.version.__version__ != "3006.27":
            self.skipTest("Integration is pinned to Salt 3006.27")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            states = root / "states"
            (states / "roles").mkdir(parents=True)
            (states / "roles" / "client_stack.sls").write_text(
                "private-state-name:\n  test.nop:\n    - name: SECRET\n    - fire_event: true\n"
            )
            marker = root / "parallel-must-not-run"
            (states / "roles" / "client_credentials.sls").write_text(
                "private-parallel-state:\n  cmd.run:\n"
                f"    - name: /bin/touch {marker}\n    - parallel: true\n"
            )
            opts = salt.config.minion_config(None)
            opts.update(
                id=MINION,
                file_client="local",
                cachedir=str(root / "cache"),
                pki_dir=str(root / "pki"),
                sock_dir=str(root / "sock"),
                extension_modules=str(root / "extmods"),
                file_roots={"base": [str(states)]},
                pillar_roots={"base": []},
                cache_jobs=False,
                minion_pillar_cache=False,
                multiprocessing=True,
                state_events=True,
                grains={},
                test=False,
            )
            for name in ("cache", "pki", "sock", "extmods"):
                (root / name).mkdir(mode=0o700)
            module_dir = root / "extmods" / "modules"
            module_dir.mkdir()
            module_dir.joinpath("oduflow_job.py").write_text(
                (ROOT / "salt/minion/extmods/modules/oduflow_job.py").read_text()
            )
            functions = salt.loader.minion_mods(opts, utils=salt.loader.utils(opts))
            loaded_run = functions["oduflow_job.run"]
            publisher = {
                "jid": JID,
                "fun": "oduflow_job.run",
                "arg": [REQUEST, "configure"],
                "tgt": [MINION],
                "tgt_type": "list",
                "ret": "",
                "user": "root",
                "master_id": "master",
                "metadata": {},
            }
            args, kwargs = salt.minion.load_args_and_kwargs(
                loaded_run,
                [REQUEST, "configure", {"__kwarg__": True, "__pub_jid": "forged"}],
                publisher,
            )
            self.assertEqual(kwargs["__pub_jid"], JID)
            event = Mock()
            with (
                patch.dict(
                    loaded_run.__globals__, {"_ROOT": root / "receipts", "_OWNER_UID": os.getuid()}
                ),
                patch.object(job.os, "geteuid", return_value=0),
                patch.object(salt.state.State, "event", event),
            ):
                self.assertEqual(functions["oduflow_job.capability"]()["minion_id"], MINION)
                result = loaded_run(*args, **kwargs)
                self.assertEqual((result["status"], result["total"]), ("succeeded", 1))
                self.assertFalse(list((root / "cache").glob("*.p")))
                event.assert_not_called()
                payload = json.dumps(
                    {
                        "secret-custom-state": {
                            "test.nop": [{"name": "SECRET"}, {"fire_event": True}]
                        }
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                custom = loaded_run(
                    SECOND, "custom", state_data_json=payload, __pub_jid="20260912010000123457"
                )
                self.assertEqual((custom["status"], custom["total"]), ("succeeded", 1))
                parallel = loaded_run(
                    "683a74a5-9ef6-4512-995a-9d6338c008bd",
                    "credentials",
                    __pub_jid="20260912010000123458",
                )
                self.assertIn(parallel["status"], ("failed", "unknown"))
                self.assertFalse(marker.exists())
                self.assertFalse(list((root / "cache").glob("*.p")))
                event.assert_not_called()
                for path in (root / "receipts").glob("*.json"):
                    self.assertNotIn("SECRET", path.read_text())
                    self.assertNotIn("private-state-name", path.read_text())


if __name__ == "__main__":
    unittest.main()
