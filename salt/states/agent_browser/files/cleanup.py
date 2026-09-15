#!/usr/bin/python3
"""Terminate browser processes of one client user without matching command text."""

import argparse
import json
import os
import pwd
import signal
import time
from dataclasses import dataclass
from pathlib import Path

PROC = Path("/proc")
BROWSERS = {
    "agent-browser",
    "agent-browser-linux-x64",
    "agent-browser-linux-arm64",
    "agent-browser-linux-musl-x64",
    "agent-browser-linux-musl-arm64",
    "chrome",
    "chromium",
    "chromium-browser",
    "chrome-headless-shell",
    "headless_shell",
    "chrome_crashpad_handler",
}


@dataclass(frozen=True)
class Process:
    pid: int
    uid: int
    parent: int
    started: int
    executable: str


def process(pid):
    directory = PROC / str(pid)
    try:
        # comm can contain spaces and parentheses; fields after it are fixed.
        fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return Process(
            pid,
            directory.stat().st_uid,
            int(fields[1]),
            int(fields[19]),
            Path(os.readlink(directory / "exe").removesuffix(" (deleted)")).name,
        )
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def browser_processes(uid):
    processes = {}
    for directory in PROC.iterdir():
        if directory.name.isdecimal():
            item = process(int(directory.name))
            if item and item.uid == uid:
                processes[item.pid] = item
    selected = {pid for pid, item in processes.items() if item.executable in BROWSERS}
    while True:
        descendants = {pid for pid, item in processes.items() if item.parent in selected}
        expanded = selected | descendants
        if expanded == selected:
            return [processes[pid] for pid in sorted(selected)]
        selected = expanded


def send(item, sig):
    """Bind the signal to the inspected process, even if its PID gets reused."""
    try:
        descriptor = os.pidfd_open(item.pid)
    except ProcessLookupError:
        return False
    try:
        current = process(item.pid)
        if current is None or current.uid != item.uid or current.started != item.started:
            return False
        signal.pidfd_send_signal(descriptor, sig)
        return True
    except ProcessLookupError:
        return False
    finally:
        os.close(descriptor)


def alive(item):
    current = process(item.pid)
    return current is not None and current.uid == item.uid and current.started == item.started


def cleanup(uid, grace=10):
    if uid == 0:
        raise ValueError("Refusing browser cleanup for root")
    targets = browser_processes(uid)
    terminated = sum(send(item, signal.SIGTERM) for item in targets)
    deadline = time.monotonic() + grace
    while any(alive(item) for item in targets) and time.monotonic() < deadline:
        time.sleep(0.1)
    # A daemon can create children while shutting down. Include those too.
    remaining = {item.pid: item for item in targets if alive(item)}
    remaining.update({item.pid: item for item in browser_processes(uid)})
    killed = sum(send(item, signal.SIGKILL) for item in remaining.values())
    deadline = time.monotonic() + 3
    while any(alive(item) for item in remaining.values()) and time.monotonic() < deadline:
        time.sleep(0.1)
    survivors = browser_processes(uid)
    return {"terminated": terminated, "killed": killed, "remaining": len(survivors)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="paseo")
    args = parser.parse_args()
    account = pwd.getpwnam(args.user)
    if account.pw_uid == 0:
        raise ValueError("Refusing browser cleanup for root")
    # Inspect /proc as the process owner, including inside containers without
    # CAP_SYS_PTRACE, and never retain root privileges while sending signals.
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    if os.geteuid() != account.pw_uid:
        raise PermissionError("Cleanup must run as root or the selected client user")
    result = cleanup(account.pw_uid)
    # Counts only: browser arguments, page URLs and user data can contain secrets.
    print(json.dumps(result, sort_keys=True))
    return 1 if result["remaining"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
