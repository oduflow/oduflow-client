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

Tests include real Git checkouts, Salt rendering/application, receipt durability,
process death, storage refusals and application contracts. They do not prove live
cloud provisioning; live deployment evidence belongs in the platform repository.
