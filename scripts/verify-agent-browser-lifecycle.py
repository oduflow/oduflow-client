#!/usr/bin/python3
"""Verify actual OpenCode/browser lifecycles on an isolated disposable client.

Run as paseo after Salt installs the agent and browser packages. This invokes
full-user nightly cleanup; never run it on an active client. A local model
fixture asks the real OpenCode bash tool to open a real page, then finishes or
pauses so the caller can be terminated. No external model or credentials are
used. This is a process test, not live provisioning evidence.
"""

import importlib.util
import json
import os
import pwd
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The installed helper deliberately has no .py suffix.
from importlib.machinery import SourceFileLoader
from pathlib import Path

spec = importlib.util.spec_from_loader(
    "browser_cleanup",
    SourceFileLoader("browser_cleanup", "/usr/local/libexec/oduflow-browser-cleanup"),
)
cleanup = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cleanup
spec.loader.exec_module(cleanup)


class Handler(BaseHTTPRequestHandler):
    mode = "normal"
    calls = 0
    opened = threading.Event()

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = (
            b"<html><title>Oduflow browser verification</title>"
            b"<button onclick=\"this.textContent='Done'\">Click me</button></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.calls += 1
        tool_results = [m for m in request.get("messages", []) if m.get("role") == "tool"]
        if not request.get("tools"):
            delta = {"content": "Browser lifecycle test"}
            finish = "stop"
        elif tool_results:
            Handler.opened.set()
            if Handler.mode != "normal":
                time.sleep(15)
            delta = {"content": "Browser test task finished."}
            finish = "stop"
        else:
            names = [t["function"]["name"] for t in request.get("tools", [])]
            assert "bash" in names, names
            args = {
                "command": f"agent-browser --session lifecycle-{Handler.mode} open http://127.0.0.1:{self.server.server_port}/",
                "description": "Open the isolated browser test page",
            }
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "browser_open",
                        "type": "function",
                        "function": {"name": "bash", "arguments": json.dumps(args)},
                    }
                ]
            }
            finish = "tool_calls"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for change, reason in [(delta, None), ({}, finish)]:
                item = {
                    "id": "fixture",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "fixture",
                    "choices": [{"index": 0, "delta": change, "finish_reason": reason}],
                }
                self.wfile.write(("data: " + json.dumps(item) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    account = pwd.getpwnam("paseo")
    assert os.getuid() == account.pw_uid, "Run as the unprivileged paseo user"
    assert not cleanup.browser_processes(account.pw_uid), (
        "Use an isolated client with no browser sessions"
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {
        "HOME": account.pw_dir,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "OPENCODE_DISABLE_SHARE": "true",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
        "OPENCODE_CONFIG_CONTENT": json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "model": "fixture/fixture",
                "small_model": "fixture/fixture",
                "enabled_providers": ["fixture"],
                "provider": {
                    "fixture": {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": "fixture",
                        "options": {
                            "baseURL": f"http://127.0.0.1:{server.server_port}/v1",
                            "apiKey": "local-fixture",
                        },
                        "models": {
                            "fixture": {
                                "name": "fixture",
                                "limit": {"context": 32000, "output": 1000},
                            }
                        },
                    }
                },
                "permission": {"bash": "allow"},
            }
        ),
    }
    for mode in ("normal", "term", "kill"):
        Handler.mode = mode
        Handler.calls = 0
        Handler.opened.clear()
        log = tempfile.TemporaryFile(mode="w+")
        p = subprocess.Popen(
            [
                "opencode",
                "run",
                "--format",
                "json",
                "Open the local test page using bash, then finish. This is a lifecycle fixture.",
            ],
            env=env,
            cwd="/tmp",
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            if mode == "normal":
                code = p.wait(timeout=60)
                assert code == 0, ("OpenCode failed", code)
                # CLI event output can end before the final text is flushed.
                # Verify the stored turn finished successfully, not just exit 0.
                log.seek(0)
                events = [json.loads(line) for line in log if line.startswith("{")]
                session_id = next(event["sessionID"] for event in events if "sessionID" in event)
                exported = json.loads(
                    subprocess.check_output(
                        ["opencode", "export", session_id],
                        env=env,
                        text=True,
                        stderr=subprocess.DEVNULL,
                    )
                )
                final = exported["messages"][-1]
                assert final["info"].get("finish") == "stop", final["info"]
                assert not final["info"].get("error"), final["info"]
            else:
                assert Handler.opened.wait(45), "OpenCode did not execute the browser command"
                p.send_signal(signal.SIGTERM if mode == "term" else signal.SIGKILL)
                p.wait(timeout=10)
            processes = cleanup.browser_processes(account.pw_uid)
            print(
                json.dumps(
                    {
                        "case": mode,
                        "opencode_exit": p.returncode,
                        "model_requests": Handler.calls,
                        "remaining_browsers": len(processes),
                        "executables": sorted({x.executable for x in processes}),
                    }
                ),
                flush=True,
            )
            assert any(x.executable == "chrome" for x in processes), (
                "Expected browser to outlive caller"
            )
            session = f"lifecycle-{mode}"

            def browser(*args):
                return subprocess.check_output(
                    ["agent-browser", "--session", session, *args], env=env, text=True
                )

            assert "Click me" in browser("snapshot", "-i")
            browser("click", "@e1")
            assert "Done" in browser("get", "text", "body")
            with tempfile.TemporaryDirectory(prefix="browser-shot-") as directory:
                screenshot = Path(directory) / "page.png"
                browser("screenshot", str(screenshot))
                assert screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            result = subprocess.run(
                ["/usr/local/libexec/oduflow-browser-cleanup", "--user", "paseo"],
                capture_output=True,
                text=True,
                check=True,
            )
            print("nightly_cleanup", result.stdout.strip(), flush=True)
            assert not cleanup.browser_processes(account.pw_uid)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            if cleanup.browser_processes(account.pw_uid):
                subprocess.run(
                    ["/usr/local/libexec/oduflow-browser-cleanup", "--user", "paseo"], check=True
                )
            log.close()
    for session in ("explicit-close", "idle-expiry"):
        browser_env = dict(
            env, AGENT_BROWSER_IDLE_TIMEOUT_MS="2000" if session == "idle-expiry" else "900000"
        )
        command = ["agent-browser", "--session", session]
        try:
            subprocess.run(
                [*command, "open", f"http://127.0.0.1:{server.server_port}/"],
                env=browser_env,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            if session == "explicit-close":
                subprocess.run(
                    [*command, "close"], env=browser_env, check=True, stdout=subprocess.DEVNULL
                )
            deadline = time.monotonic() + 15
            while cleanup.browser_processes(account.pw_uid) and time.monotonic() < deadline:
                time.sleep(0.1)
            assert not cleanup.browser_processes(account.pw_uid), session
            print(session + ": no remaining browser processes", flush=True)
        finally:
            subprocess.run(
                ["/usr/local/libexec/oduflow-browser-cleanup", "--user", "paseo"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
    server.shutdown()


if __name__ == "__main__":
    main()
