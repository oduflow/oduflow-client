# Oduflow Client

Versioned Salt configuration for a client VM: Salt Minion, Tailscale, Oduflow,
IDE, storage, client agents, SSH, backups, and the client's production Odoo stack.
The control Odoo addons, cloud provider APIs, Salt Master, Headscale server and
LiteLLM infrastructure live in `oduflow/oduflow-platform`.

## Install and update

The platform selects a full 40-character Git commit SHA. Cloud-init checks out
that exact commit into `/opt/oduflow/client/releases/<SHA>` and invokes
`bootstrap.sh UUID MASTER FINGERPRINT INPUT_DIRECTORY`. The input directory
contains private, one-use VPN and minion enrollment files. Client release checkouts
use public HTTPS and require no repository credentials.

After enrollment, the platform queues `oduflow_job.run` with a fixed operation
profile and the selected SHA. The client checks out the release and applies its
local Salt states. Pillar comes from the authenticated Salt Master and remains
outside the checkout. The durable request receipt binds the UUID, Salt JID,
profile and SHA before state execution. A lost response never permits a second
execution of the same request. Modified release checkouts are rejected.

Select a SHA in the platform's **Desired Client Revision** field and use
**Apply Configuration** to enqueue an update. Successful configuration includes
application health checks; only then are applied and verified revisions recorded.
Production publication has its own readiness checks. Inspect local revision
metadata with `salt-call oduflow_job.version`; it contains no pillar values.

`release.json` defines the compatibility contract and a readable version;
the commit SHA is authoritative. The current contract requires Salt 3006.27 and
pillar schema 1. Existing receipts without a revision remain historical records.
Selecting an older SHA is not a database or filesystem rollback.

## Scheduled maintenance

Salt enables `paseo-nightly-restart.timer` when starting client applications.
Daily at 04:00 in the client VM's local timezone, it terminates Agent Browser,
Chrome/Chromium and their descendants owned by `paseo`, then restarts the running
Paseo daemon. Cleanup sends SIGTERM, allows 10 seconds for shutdown, then sends
SIGKILL to survivors. It matches executable names and user IDs, never command-line
substrings, and binds signals to process handles to prevent PID reuse races.
Missed runs during downtime are skipped. A stopped Paseo daemon stays stopped.
Maintenance interrupts active browser tests, Paseo connections and agent work.

## Browser automation

The credential-free `agent_browser.install` Salt state installs checksum-pinned
Agent Browser and Chrome for Testing, including Linux libraries. It runs both
for fresh clients and in the Packer image build. `agent-browser` is on the common
CLI path; the shared browser binaries live under `/opt/oduflow`. Persistent user
state lives under the `paseo` home on the verified client data volume; transient
Chrome profiles use the system temporary directory.

Client configuration installs the `agent-browser` skill for OpenCode
(`~/.config/opencode/skills`), Claude (`~/.claude/skills`) and Codex
(`~/.agents/skills`). These point to one managed discovery guide, which loads
the version-matched upstream instructions with `agent-browser skills get core`.
Other skills and agent configuration files are preserved.

Each task should use a unique browser session and explicitly close it on success
or failure. The browser daemon survives its calling agent; completing a turn,
SIGTERM and SIGKILL are not session cleanup. The managed command defaults to a
15-minute idle timeout through `AGENT_BROWSER_IDLE_TIMEOUT_MS`; explicit session
overrides remain available. Nightly cleanup is the final fallback.

## Dependencies and images

Application versions and checksums live in `salt/states/client_apps/artifacts.json`.
The checksum-pinned IDE source archive is included under
`salt/states/client_apps/artifacts/`, so a clean client requires no access to the
Paseo or platform repositories. Ubuntu packages still resolve through signed
Ubuntu repositories; a Git SHA alone does not freeze those repositories.

`packer/client.pkr.hcl` builds an optional identity-free Ubuntu image from this
repository's Salt tree. Build from a clean checkout and pass the exact client SHA
as `source_revision`. Images accelerate installation; enrollment and configuration
still run from the selected client release after boot.

## Verification

```sh
python3 -m pip install -r requirements-test.txt
ruff check salt scripts tests
ruff format --check salt scripts tests
python3 -m unittest discover -s tests -v
python3 scripts/validate-salt-minion.py
```

On a disposable client with the packages installed, run
`python3 scripts/verify-agent-browser-lifecycle.py` as `paseo`. This uses actual
OpenCode, Agent Browser and Chrome with a local model fixture, checks page
interaction and screenshots, and exercises normal exit, SIGTERM, SIGKILL,
explicit close, idle expiry and nightly cleanup. It intentionally terminates
browser processes of that user, so it must not run on an active client.

Tests include real Git checkouts, Salt rendering/application, receipt durability,
process death, storage refusals and application contracts. They do not prove live
cloud provisioning; live deployment evidence belongs in the platform repository.
