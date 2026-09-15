---
name: agent-browser
description: Use Agent Browser to inspect websites, interact with forms, take screenshots, and verify web UI behavior on this client VM.
---

# Agent Browser

Agent Browser and Chrome for Testing are installed by Salt. Load the guide that
matches the installed CLI with `agent-browser skills get core`. Use
`agent-browser skills get core --full` for the full reference, or
`agent-browser skills get dogfood` for exploratory UI testing.

Use a unique named session for each independent task and reuse its name on every
command, for example `agent-browser --session ui-<task-id> open <url>`.
Inspect `snapshot -i`, interact using its refs, and take a new snapshot after
navigation or page changes. Save screenshots in the task's workspace.

Always close your session with `agent-browser --session ui-<task-id> close` when
finished, including after a failed test. In scripts, use a `finally` block or
an EXIT/INT/TERM trap. Closing a shell or finishing an agent turn does not close
the detached browser daemon. Do not use `close --all` or process-wide kills for
ordinary task cleanup: other agents may have active sessions.

The managed command defaults to a 15-minute idle timeout as a fallback for
interrupted tasks. Nightly maintenance at 04:00 VM time terminates remaining
Agent Browser and Chromium processes owned by `paseo` and restarts Paseo.
