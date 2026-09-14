"""Exercise durable receipt boundaries with real worker death and OS file locks.

State work is a local marker write; these tests prove the minion receipt protocol,
not network transport or live provisioning. SIGKILL prevents Python cleanup from
making an interrupted worker look safer than an actual process failure.
"""

import importlib.util
import json
import os
import select
import signal
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "receipt_protocol_job", ROOT / "salt/minion/extmods/modules/oduflow_job.py"
)
job = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(job)
MINION = "client-b6d892dc-1c11-49b2-9785-db159c1a53be"
REQUEST = "a651631d-db40-45fd-a8db-7d520933b835"
SECOND_REQUEST = "fcafcbd2-95b2-4190-bd90-45c973dfdb54"
JID = "20260912020000123456"
SECOND_JID = "20260912020000123457"


@unittest.skipUnless(hasattr(os, "fork"), "Receipt worker death tests require POSIX fork")
class SaltReceiptProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.receipts = self.directory / "receipts"
        self.marker = self.directory / "state-executions"
        self.worker_pids = set()
        for patcher in (
            patch.object(job, "_ROOT", self.receipts),
            patch.object(job, "_OWNER_UID", os.getuid()),
            patch.object(job.os, "geteuid", return_value=0),
            patch.object(job, "_execution_safety", side_effect=lambda: nullcontext()),
            patch.object(job, "_supported_runtime", return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        job.__opts__ = {"id": MINION, "cache_jobs": False, "minion_pillar_cache": False}
        job.__salt__ = {"state.apply": self.execute}

    def execute(self, *args, **kwargs):
        with self.marker.open("ab") as stream:
            stream.write(b"one execution\n")
            stream.flush()
            os.fsync(stream.fileno())
        return {"PRIVATE-state": {"result": True, "changes": {"secret": "CANARY"}}}

    def run_job(self, request=REQUEST, jid=JID):
        return job.run(request, "configure", __pub_jid=jid)

    def execution_count(self):
        return len(self.marker.read_bytes().splitlines()) if self.marker.exists() else 0

    def start_paused_worker(self, boundary):
        reader, writer = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(reader)

            def pause_here():
                os.write(writer, b"R")
                while True:
                    signal.pause()

            original_write = job._write

            def write(directory, name, data):
                original_write(directory, name, data)
                if boundary == "index_committed" and name.startswith("jid-"):
                    pause_here()
                if boundary == "terminal_committed" and data.get("status") == "succeeded":
                    pause_here()

            def execute(*args, **kwargs):
                value = self.execute(*args, **kwargs)
                if boundary == "state_side_effect":
                    pause_here()
                return value

            job._write = write
            job.__salt__ = {"state.apply": execute}
            try:
                self.run_job()
                if boundary == "before_publication":
                    pause_here()
                os.write(writer, b"E")
            except BaseException:
                os.write(writer, b"E")
            finally:
                os._exit(1)
        os.close(writer)
        self.worker_pids.add(pid)
        self.addCleanup(self.stop_worker, pid)
        try:
            ready, _, _ = select.select([reader], [], [], 10)
            self.assertTrue(ready, "Worker did not reach the controlled receipt boundary")
            self.assertEqual(os.read(reader, 1), b"R", "Worker failed before receipt boundary")
        finally:
            os.close(reader)
        return pid

    def stop_worker(self, pid):
        if pid not in self.worker_pids:
            return
        self.worker_pids.remove(pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    def test_kill_after_partial_claim_never_runs_same_publication(self):
        pid = self.start_paused_worker("index_committed")
        self.stop_worker(pid)
        self.assertEqual(job.status(JID, REQUEST, "configure")["status"], "unknown")
        self.assertEqual(self.run_job()["status"], "unknown")
        self.assertEqual(self.execution_count(), 0)

    def test_kill_after_side_effect_stays_unknown_without_reexecution(self):
        pid = self.start_paused_worker("state_side_effect")
        self.assertEqual(job.status(JID, REQUEST, "configure")["status"], "running")
        self.stop_worker(pid)
        self.assertEqual(job.status(JID, REQUEST, "configure")["status"], "unknown")
        self.assertEqual(self.run_job()["status"], "unknown")
        self.assertEqual(self.execution_count(), 1)
        with self.assertRaisesRegex(job._JobError, "binding_conflict"):
            self.run_job(jid=SECOND_JID)
        self.assertEqual(self.execution_count(), 1)

    def test_kill_after_terminal_commit_preserves_exact_recoverable_result(self):
        pid = self.start_paused_worker("terminal_committed")
        self.stop_worker(pid)
        result = job.status(JID, REQUEST, "configure")
        self.assertEqual((result["status"], result["passed"], result["total"]), ("succeeded", 1, 1))
        self.assertEqual(self.run_job(), result)
        self.assertEqual(self.execution_count(), 1)

    def test_lost_publication_after_return_recovers_without_second_execution(self):
        pid = self.start_paused_worker("before_publication")
        self.stop_worker(pid)
        result = job.status(JID, REQUEST, "configure")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.run_job(), result)
        self.assertEqual(self.execution_count(), 1)
        for path in self.receipts.iterdir():
            self.assertNotIn(b"CANARY", path.read_bytes())
            self.assertNotIn(b"PRIVATE-state", path.read_bytes())

    def test_duplicate_worker_and_concurrent_poll_do_not_execute_or_block(self):
        pid = self.start_paused_worker("state_side_effect")
        for _ in range(3):
            self.assertEqual(job.status(JID, REQUEST, "configure")["status"], "running")
            self.assertEqual(self.run_job()["status"], "running")
        self.assertEqual(self.run_job(SECOND_REQUEST, SECOND_JID)["status"], "unknown")
        self.assertEqual(self.execution_count(), 1)
        self.assertFalse((self.receipts / f"request-{SECOND_REQUEST}.json").exists())
        self.stop_worker(pid)
        self.assertEqual(job.status(JID, REQUEST, "configure")["status"], "unknown")

    def test_poll_rejects_changed_identity_profile_or_request(self):
        self.run_job()
        for request, profile in ((SECOND_REQUEST, "configure"), (REQUEST, "production")):
            with self.subTest(request=request, profile=profile):
                with self.assertRaisesRegex(job._JobError, "binding_conflict"):
                    job.status(JID, request, profile)
        with patch.dict(job.__opts__, {"id": "client-fcafcbd2-95b2-4190-bd90-45c973dfdb54"}):
            with self.assertRaisesRegex(job._JobError, "binding_conflict"):
                job.status(JID, REQUEST, "configure")
        self.assertEqual(self.execution_count(), 1)

    def test_poll_refuses_symlinked_lock_and_foreign_receipt_permissions(self):
        self.run_job()
        lock = self.receipts / f"request-{REQUEST}.lock"
        lock.unlink()
        target = self.directory / "unrelated"
        target.write_text("untouched")
        lock.symlink_to(target)
        with self.assertRaises(job._JobError):
            job.status(JID, REQUEST, "configure")
        self.assertEqual(target.read_text(), "untouched")
        lock.unlink()
        lock.touch(mode=0o600)
        receipt = self.receipts / f"request-{REQUEST}.json"
        receipt.chmod(0o644)
        with self.assertRaises(job._JobError):
            job.status(JID, REQUEST, "configure")
        self.assertEqual(self.execution_count(), 1)

    def test_foreign_owner_metadata_is_refused_for_receipt_and_directory(self):
        self.run_job()
        original_fstat = os.fstat
        for path in (self.receipts, self.receipts / f"request-{REQUEST}.json"):
            expected_inode = path.stat().st_ino

            def foreign_owner(fd):
                metadata = original_fstat(fd)
                if metadata.st_ino == expected_inode:
                    values = list(metadata)
                    values[4] = os.getuid() + 10000
                    return os.stat_result(values)
                return metadata

            with self.subTest(path=path.name), patch.object(job.os, "fstat", foreign_owner):
                with self.assertRaisesRegex(job._JobError, "storage_invalid"):
                    job.status(JID, REQUEST, "configure")
        self.assertEqual(self.execution_count(), 1)

    def test_damaged_terminal_counts_never_become_success(self):
        self.run_job()
        path = self.receipts / f"request-{REQUEST}.json"
        receipt = json.loads(path.read_text())
        for changes in ({"passed": True}, {"total": 0}, {"status": "failed"}, {"extra": "CANARY"}):
            with self.subTest(changes=changes):
                path.write_text(json.dumps(dict(receipt, **changes)))
                with self.assertRaises(job._JobError):
                    job.status(JID, REQUEST, "configure")
        self.assertEqual(self.execution_count(), 1)


if __name__ == "__main__":
    unittest.main()
