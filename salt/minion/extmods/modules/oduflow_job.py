"""Fixed-role state execution with private, pre-publication durable receipts.

Only reduced counts leave this module. A request UUID and Salt publication JID
are permanently bound before state execution. An abandoned claim never permits
another execution, even when no terminal result survived.
"""

import fcntl
import hashlib
import io
import json
import os
import re
import stat
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from uuid import UUID, uuid4

__virtualname__ = "oduflow_job"
__opts__ = {}
__salt__ = {}
_ROOT = Path("/var/lib/oduflow/job-receipts")
_OWNER_UID = 0
_PROFILES = {
    "configure": "roles.client_stack",
    "production": "roles.client_production",
    "volume_resize": "roles.client_storage_resize",
    "credentials": "roles.client_credentials",
    "custom": "custom.highstate",
    "litellm": "roles.litellm",
    "litellm_sync": "litellm_metering.sync",
}
_FIELDS = {"protocol", "minion_id", "request_id", "jid", "profile", "state_digest"}

# Diagnostics are a separate versioned surface, never completion/replay evidence.
_DIAGNOSTIC_PHASES = {
    "setup",
    "release_prepare",
    "states_execute",
    "result_reduce",
    "result_persist",
    "completed",
}
_DIAGNOSTIC_REASONS = {"in_progress", "exception", "invalid_result", "states_failed", "completed"}
_DIAGNOSTIC_ERRORS = {
    "ValueError",
    "TypeError",
    "KeyError",
    "OSError",
    "RuntimeError",
    "OtherException",
    "",
}
_DIAGNOSTIC_CODES = {
    "",
    "client_repository_git_failed",
    "client_release_checkout_modified",
    "client_release_pillar_identity_invalid",
    "client_repository_path_unsafe",
    "client_release_contract_unsupported",
    "client_repository_host_keys_invalid",
    "oduflow_job_state_cache_read_refused",
    "oduflow_job_parallel_unsupported",
}


def _validate_diagnostic(value):
    choices = {
        "phase": {
            "setup",
            "release_prepare",
            "states_execute",
            "result_reduce",
            "result_persist",
            "completed",
        },
        "reason": {"in_progress", "exception", "invalid_result", "states_failed", "completed"},
        "error_type": {
            "",
            "ValueError",
            "TypeError",
            "KeyError",
            "OSError",
            "RuntimeError",
            "OtherException",
        },
        "code": {
            "",
            "client_repository_git_failed",
            "client_release_checkout_modified",
            "client_release_pillar_identity_invalid",
            "client_repository_path_unsafe",
            "client_release_contract_unsupported",
            "client_repository_host_keys_invalid",
            "oduflow_job_state_cache_read_refused",
            "oduflow_job_parallel_unsupported",
        },
    }
    if not isinstance(value, dict) or set(value) != set(choices) | {
        "schema",
        "started_at",
        "updated_at",
        "frames",
    }:
        return None
    if type(value["schema"]) is not int or value["schema"] != 1:
        return None
    if any(
        type(value[key]) is not str or value[key] not in allowed for key, allowed in choices.items()
    ):
        return None
    if any(
        type(value[key]) is not int or not 0 < value[key] < 2**40
        for key in ("started_at", "updated_at")
    ):
        return None
    if value["updated_at"] < value["started_at"]:
        return None
    frames = value["frames"]
    if not isinstance(frames, list) or len(frames) > 8:
        return None
    for frame in frames:
        if not isinstance(frame, dict) or set(frame) != {"module", "line"}:
            return None
        if frame["module"] not in ("oduflow_job.py", "oduflow_release.py"):
            return None
        if type(frame["line"]) is not int or not 0 < frame["line"] < 100000:
            return None
    return value


def _diagnostic(directory, binding, phase, reason="in_progress", exc=None, started_at=None):
    """Build from constants; never serialize exception text, locals or Salt returns."""
    now = int(time.time())
    error_type, code, frames = "", "", []
    if exc is not None:
        error_type = type(exc).__name__
        if error_type not in _DIAGNOSTIC_ERRORS:
            error_type = "OtherException"
        if len(exc.args) == 1 and type(exc.args[0]) is str and exc.args[0] in _DIAGNOSTIC_CODES:
            code = exc.args[0]
        trace = exc.__traceback__
        while trace:
            # Only our fixed source names and numeric locations leave the host.
            filename = Path(trace.tb_frame.f_code.co_filename).name
            if filename in {"oduflow_job.py", "oduflow_release.py"}:
                frames.append({"module": filename, "line": trace.tb_lineno})
            trace = trace.tb_next
    value = dict(
        schema=1,
        phase=phase,
        reason=reason,
        error_type=error_type,
        code=code,
        frames=frames[-8:],
        started_at=started_at or now,
        updated_at=now,
    )
    try:
        _write(
            directory,
            "diagnostic-" + binding["request_id"] + ".json",
            {"binding": binding, "diagnostic": value},
        )
    except Exception:
        # Optional evidence must not mask the execution or terminal receipt.
        pass


def diagnostics(
    jid, request_id, profile, state_digest="", client_revision="", source_digest="", **kwargs
):
    """Read optional evidence without changing the durable execution receipt."""
    _metadata(kwargs)
    binding = _binding(jid, request_id, profile, state_digest, client_revision, source_digest)
    with _directory(False) as directory:
        if directory is None:
            return {}
        if _existing(directory, binding, live=_running(directory, request_id)) is None:
            return {}
        value = _read(directory, "diagnostic-" + request_id + ".json")
        if value is None:
            return {}
        if (
            not isinstance(value, dict)
            or set(value) != {"binding", "diagnostic"}
            or value.get("binding") != binding
            or not _validate_diagnostic(value.get("diagnostic"))
        ):
            raise _JobError("oduflow_job_binding_conflict")
        return value


class _JobError(RuntimeError):
    pass


def __virtual__():
    return __virtualname__


def _minion():
    value = __opts__.get("id", "")
    if (
        os.geteuid() != 0
        or not isinstance(value, str)
        or not re.fullmatch(r"client-[0-9a-f-]{36}", value)
    ):
        raise _JobError("oduflow_job_identity_invalid")
    try:
        if str(UUID(value[7:])) != value[7:]:
            raise ValueError
    except ValueError:
        raise _JobError("oduflow_job_identity_invalid") from None
    return value


def _metadata(values):
    if any(not key.startswith("__pub_") for key in values):
        raise _JobError("oduflow_job_arguments_invalid")


def _binding(jid, request_id, profile, digest="", client_revision="", source_digest=""):
    try:
        if not isinstance(jid, str) or not re.fullmatch(r"[0-9]{20}", jid):
            raise ValueError
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
            raise ValueError
        if profile not in _PROFILES:
            raise ValueError
        if (profile == "custom" and not re.fullmatch(r"[0-9a-f]{64}", digest)) or (
            profile != "custom" and digest != ""
        ):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise _JobError("oduflow_job_identity_invalid") from None
    if client_revision and not re.fullmatch(r"[0-9a-f]{40}", client_revision):
        raise _JobError("oduflow_job_revision_invalid")
    if source_digest and (
        profile not in ("litellm", "litellm_sync")
        or client_revision
        or not re.fullmatch(r"[0-9a-f]{64}", source_digest)
    ):
        raise _JobError("oduflow_job_sources_invalid")
    return dict(
        **({"source_digest": source_digest} if source_digest else {}),
        **({"client_revision": client_revision} if client_revision else {}),
        protocol=3 if source_digest else (2 if client_revision else 1),
        minion_id=_minion(),
        request_id=request_id,
        jid=jid,
        profile=profile,
        state_digest=digest,
    )


def _payload(value):
    """Accept the master's canonical bounded high-data contract, never render SLS."""
    try:
        if not isinstance(value, str) or len(value.encode()) > 131072:
            raise ValueError
        data = json.loads(value)
        if not isinstance(data, dict) or not 1 <= len(data) <= 100:
            raise ValueError
        if any(marker in value for marker in ("{{", "{%", "{#", "#!")):
            raise ValueError
        for name, functions in data.items():
            if not name or name.startswith("__") or name in {"include", "exclude", "extend"}:
                raise ValueError
            if not isinstance(functions, dict) or not functions:
                raise ValueError
            if any(
                not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", key)
                or not isinstance(args, list)
                for key, args in functions.items()
            ):
                raise ValueError
            modules = [function.split(".")[0] for function in functions]
            if len(set(modules)) != len(modules):
                raise ValueError
            if any(
                not isinstance(argument, dict) or len(argument) != 1
                for args in functions.values()
                for argument in args
            ):
                raise ValueError
            if any("parallel" in argument for args in functions.values() for argument in args):
                raise ValueError
        if (
            json.dumps(
                data, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
            )
            != value
        ):
            raise ValueError
        high = {
            name: {
                function.split(".")[0]: [function.split(".")[1], *args]
                for function, args in functions.items()
            }
            for name, functions in data.items()
        }
        return high, hashlib.sha256(value.encode()).hexdigest()
    except (ValueError, TypeError, RecursionError):
        raise _JobError("oduflow_job_custom_invalid") from None


@contextmanager
def _directory(create):
    """Traverse trusted directories with openat/O_NOFOLLOW; pin the receipt directory."""
    if not _ROOT.is_absolute() or ".." in _ROOT.parts:
        raise _JobError("oduflow_job_storage_invalid")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, name in enumerate(_ROOT.parts[1:]):
            last = index == len(_ROOT.parts) - 2
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                os.mkdir(name, 0o700, dir_fd=fd)
                os.fsync(fd)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            info = os.fstat(child)
            mode = stat.S_IMODE(info.st_mode)
            # Root-owned sticky ancestors (e.g. /tmp in tests) cannot replace a
            # different owner's private child. The actual receipt directory is 0700.
            ancestor_safe = info.st_uid in (0, _OWNER_UID) and (
                not mode & 0o022 or (info.st_uid == 0 and bool(mode & stat.S_ISVTX))
            )
            if not ancestor_safe or (last and (info.st_uid != _OWNER_UID or mode != 0o700)):
                os.close(child)
                raise _JobError("oduflow_job_storage_invalid")
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _private(fd):
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != _OWNER_UID
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise _JobError("oduflow_job_storage_invalid")


def _read(directory, name):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        _private(fd)
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise _JobError("oduflow_job_receipt_invalid")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise _JobError("oduflow_job_receipt_invalid")
        return data
    finally:
        os.close(fd)


def _write(directory, name, data):
    # Validate an existing destination before replacement: no links or foreign files.
    _read(directory, name)
    temporary = "." + uuid4().hex + ".tmp"
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        _private(fd)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(json.dumps(data, sort_keys=True, separators=(",", ":")).encode())
            stream.flush()
            os.fsync(fd)
        os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _lock(directory, name="execution.lock", create=True):
    fd = os.open(
        name,
        os.O_RDWR | (os.O_CREAT if create else 0) | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
        dir_fd=directory,
    )
    try:
        _private(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd
    except BaseException:
        os.close(fd)
        raise


def _running(directory, request_id):
    try:
        lock = _lock(directory, "request-" + request_id + ".lock", create=False)
    except FileNotFoundError:
        return False
    if lock is None:
        return True
    os.close(lock)
    return False


def _result(binding, status, passed=0, total=0):
    return dict(binding, status=status, passed=passed, total=total)


def _existing(directory, binding, live):
    index = _read(directory, "jid-" + binding["jid"] + ".json")
    receipt = _read(directory, "request-" + binding["request_id"] + ".json")
    for value in (index, receipt):
        if value is not None and any(value.get(key) != binding[key] for key in binding):
            raise _JobError("oduflow_job_binding_conflict")
    if index is None and receipt is None:
        return None
    if index is None or receipt is None:
        return _result(binding, "unknown")
    if set(index) != set(binding) or set(receipt) != set(binding) | {"status", "passed", "total"}:
        raise _JobError("oduflow_job_receipt_invalid")
    status, passed, total = (receipt[key] for key in ("status", "passed", "total"))
    if (
        status not in ("running", "unknown", "succeeded", "failed")
        or type(passed) is not int
        or type(total) is not int
        or not 0 <= passed <= total <= 100000
    ):
        raise _JobError("oduflow_job_receipt_invalid")
    if status == "succeeded" and (not total or passed != total):
        raise _JobError("oduflow_job_receipt_invalid")
    if status == "failed" and (not total or passed >= total):
        raise _JobError("oduflow_job_receipt_invalid")
    if status in ("running", "unknown") and (passed or total):
        raise _JobError("oduflow_job_receipt_invalid")
    return _result(
        binding,
        ("running" if live else "unknown") if status == "running" else status,
        passed,
        total,
    )


def _execution_options():
    if __opts__.get("test") or __opts__.get("cache_jobs") or __opts__.get("minion_pillar_cache"):
        raise _JobError("oduflow_job_unsafe_options")


def _supported_runtime():
    import salt.version

    if salt.version.__version__ != "3006.27" or __opts__.get("multiprocessing", True) is not True:
        raise _JobError("oduflow_job_runtime_unsupported")


@contextmanager
def _execution_safety():
    """Suppress pinned Salt's automatic raw result caches and per-state events.

    Salt 3006.27 state.sls writes sls.p and *.cache.p unconditionally, independent
    of cache_jobs/cache=False. Its State.event also honors per-state fire_event
    even when state_events=False. Parallel workers persist additional raw return
    files, so parallel state execution is refused before spawning. Scope these
    shims to this isolated worker and restore them before returning. They do not
    sandbox arbitrary custom commands or prove completion of background work.
    """
    import salt.state
    import salt.utils.files

    _supported_runtime()
    cachedir = Path(__opts__["cachedir"]).resolve()
    original_open = salt.utils.files.fopen
    original_event = salt.state.State.event
    original_parallel = salt.state.State.call_parallel

    def private_cache_open(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)):
            candidate = Path(os.fsdecode(path)).absolute()
            if candidate.parent.resolve() == cachedir and (
                candidate.name == "sls.p" or candidate.name.endswith(".cache.p")
            ):
                mode = args[0] if args else kwargs.get("mode", "r")
                if not any(marker in mode for marker in ("w", "a", "x")):
                    raise _JobError("oduflow_job_state_cache_read_refused")
                return io.BytesIO()
        return original_open(path, *args, **kwargs)

    def refuse_parallel(*args, **kwargs):
        # Parallel workers persist raw results in invocation-specific cache files.
        # Reject before spawning, including nested high data from fixed roles.
        raise _JobError("oduflow_job_parallel_unsupported")

    salt.utils.files.fopen = private_cache_open
    salt.state.State.event = lambda *args, **kwargs: None
    salt.state.State.call_parallel = refuse_parallel
    try:
        yield
    finally:
        salt.state.State.call_parallel = original_parallel
        salt.state.State.event = original_event
        salt.utils.files.fopen = original_open


def capability(release=False, sources=False, **kwargs):
    _metadata(kwargs)
    minion_id = _minion()
    _execution_options()
    _supported_runtime()
    return {
        "protocol": 3 if sources else (2 if release else 1),
        "minion_id": minion_id,
        "profiles": list(_PROFILES),
    }


def status(
    jid, request_id, profile, state_digest="", client_revision="", source_digest="", **kwargs
):
    _metadata(kwargs)
    binding = _binding(jid, request_id, profile, state_digest, client_revision, source_digest)
    try:
        with _directory(False) as directory:
            if directory is None:
                return _result(binding, "not_started")
            return _existing(directory, binding, live=_running(directory, request_id)) or _result(
                binding, "not_started"
            )
    except (OSError, ValueError, TypeError, RecursionError):
        raise _JobError("oduflow_job_receipt_unavailable") from None


def _reduce(binding, states):
    if (
        not isinstance(states, dict)
        or not 1 <= len(states) <= 100000
        or any(
            not isinstance(value, dict) or type(value.get("result")) is not bool
            for value in states.values()
        )
    ):
        return _result(binding, "unknown")
    passed = sum(value["result"] is True for value in states.values())
    return _result(binding, "succeeded" if passed == len(states) else "failed", passed, len(states))


def version(**kwargs):
    """Return only the locally recorded software revisions, without configuration."""
    _metadata(kwargs)
    _minion()
    with _directory(False) as directory:
        return (_read(directory, "client-version.json") if directory is not None else None) or {}


def _recovery_receipt(directory, request_id):
    if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
        raise _JobError("oduflow_job_recovery_invalid")
    previous = _read(directory, "request-" + request_id + ".json")
    if not isinstance(previous, dict) or previous.get("profile") != "configure":
        raise _JobError("oduflow_job_recovery_invalid")
    binding = _binding(
        previous.get("jid"), request_id, "configure", "", previous.get("client_revision", "")
    )
    result = _existing(directory, binding, live=_running(directory, request_id))
    if not result or result["status"] not in {"unknown", "failed"}:
        raise _JobError("oduflow_job_recovery_not_inactive")
    return binding, result


def recovery_status(jid, request_id, client_revision="", **kwargs):
    """Check one previous configure attempt while holding the execution lease."""
    _metadata(kwargs)
    expected = _binding(jid, request_id, "configure", "", client_revision)
    with _directory(False) as directory:
        if directory is None:
            return {}
        lock = _lock(directory)
        if lock is None:
            return {}
        try:
            binding, result = _recovery_receipt(directory, request_id)
            if binding != expected:
                raise _JobError("oduflow_job_binding_conflict")
            return {"receipt": result, "idle": True}
        finally:
            os.close(lock)


def run(
    request_id,
    profile,
    state_data_json=None,
    client_revision="",
    state_sources_json=None,
    recovery_of="",
    **kwargs,
):
    _metadata(kwargs)
    high, digest = _payload(state_data_json) if profile == "custom" else (None, "")
    if profile != "custom" and state_data_json is not None:
        raise _JobError("oduflow_job_arguments_invalid")
    source_digest = ""
    if state_sources_json is not None:
        if not isinstance(state_sources_json, str) or len(state_sources_json.encode()) > 524288:
            raise _JobError("oduflow_job_sources_invalid")
        source_digest = hashlib.sha256(state_sources_json.encode()).hexdigest()
    binding = _binding(
        kwargs.get("__pub_jid"), request_id, profile, digest, client_revision, source_digest
    )
    if recovery_of and (
        profile != "configure" or recovery_of == request_id or str(UUID(recovery_of)) != recovery_of
    ):
        raise _JobError("oduflow_job_recovery_invalid")
    _execution_options()
    _supported_runtime()
    try:
        with _directory(True) as directory:
            lock = _lock(directory)
            active_lock = None
            try:
                previous = _existing(directory, binding, live=_running(directory, request_id))
                if previous:
                    link = _read(directory, "recovery-" + request_id + ".json")
                    if (link or {}).get("previous_request", "") != recovery_of:
                        raise _JobError("oduflow_job_binding_conflict")
                    return previous
                if lock is None:
                    return _result(binding, "unknown")
                active_lock = _lock(directory, "request-" + request_id + ".lock")
                if active_lock is None:
                    return _result(binding, "unknown")
                if recovery_of:
                    previous_binding, _ = _recovery_receipt(directory, recovery_of)
                    _write(
                        directory,
                        "recovery-" + request_id + ".json",
                        {
                            "binding": binding,
                            "previous_request": recovery_of,
                            "previous_binding": previous_binding,
                        },
                    )
                _write(directory, "jid-" + binding["jid"] + ".json", binding)
                _write(directory, "request-" + request_id + ".json", _result(binding, "running"))
                started_at = int(time.time())
                phase = "setup"
                _diagnostic(directory, binding, phase, started_at=started_at)
                if client_revision and profile == "configure":
                    versions = _read(directory, "client-version.json") or {}
                    versions.update(desired=client_revision, request_id=request_id)
                    _write(directory, "client-version.json", versions)
                try:
                    options = dict(
                        test=False,
                        queue=False,
                        concurrent=False,
                        state_events=False,
                        __pub_jid=binding["jid"],
                    )
                    local = (
                        __salt__["oduflow_release.options"](client_revision, binding["minion_id"])
                        if client_revision
                        else nullcontext({})
                    )
                    if source_digest:
                        local = __salt__["oduflow_release.sources_options"](
                            state_sources_json, binding["minion_id"]
                        )
                    phase = "release_prepare"
                    _diagnostic(directory, binding, phase, started_at=started_at)
                    with _execution_safety(), local as release_options:
                        options.update(release_options)
                        phase = "states_execute"
                        _diagnostic(directory, binding, phase, started_at=started_at)
                        states = (
                            __salt__["state.high"](high, **options)
                            if profile == "custom"
                            else __salt__["state.apply"](_PROFILES[profile], **options)
                        )
                    phase = "result_reduce"
                    reduced = _reduce(binding, states)
                    if reduced["status"] == "unknown":
                        _diagnostic(
                            directory, binding, phase, "invalid_result", started_at=started_at
                        )
                except Exception as exc:
                    _diagnostic(directory, binding, phase, "exception", exc, started_at)
                    reduced = _result(binding, "unknown")
                try:
                    _write(directory, "request-" + request_id + ".json", reduced)
                except Exception as exc:
                    _diagnostic(directory, binding, "result_persist", "exception", exc, started_at)
                    raise
                if reduced["status"] in {"succeeded", "failed"}:
                    _diagnostic(
                        directory,
                        binding,
                        "completed",
                        "completed" if reduced["status"] == "succeeded" else "states_failed",
                        started_at=started_at,
                    )
                if client_revision and profile == "configure" and reduced["status"] == "succeeded":
                    versions.update(applied=client_revision, verified=client_revision)
                    _write(directory, "client-version.json", versions)
                return reduced
            finally:
                if active_lock is not None:
                    os.close(active_lock)
                if lock is not None:
                    os.close(lock)
    except (OSError, ValueError, TypeError, RecursionError):
        raise _JobError("oduflow_job_receipt_unavailable") from None
