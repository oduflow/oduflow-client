# Repository instructions

Use English for documentation, source comments and UI labels. Respond in the
user's chosen language. Keep this repository limited to software that belongs
on a client VM; platform orchestration belongs in oduflow-platform.

## Repository layout

All paths below are relative to this repository, including when it is checked
out as the platform's `client/` submodule.

| Path | Responsibility |
| --- | --- |
| `bootstrap.sh` | Entry point invoked from the selected release checkout for machine enrollment. |
| `release.json` | Readable release version, compatibility contract, Salt version and pillar schema. The Git commit SHA identifies the release source. |
| `salt/minion/` | Minion installation, enrollment and image cleanup; `extmods/modules/` contains release checkout and durable job execution modules. |
| `salt/states/` | Client packages, storage, transport, applications, agents, SSH, backups and production-stack configuration. |
| `salt/states/roles/` | Managed entry states that compose client installation and operational profiles. |
| `salt/states/client_apps/artifacts.json`, `salt/states/client_apps/artifacts/` | Pinned application versions, checksums and bundled installation artifacts. |
| `packer/` | Optional client VM image recipe and build/cleanup scripts. |
| `scripts/` | Client validation, artifact preparation and production verification tools. |
| `tests/`, `requirements-test.txt` | Client tests and their pinned Python dependencies. |
| `.github/workflows/` | Client release CI. |

See `README.md` for installation and release behavior. When working through the
platform submodule, commit and push client changes here first, then update the
platform's gitlink and `addons/oduflow/data/client-release.json` together. Keep
control Odoo addons, cloud provider operations and infrastructure server states
in the platform repository.

## Configuration and data

Salt is the configuration authority. Releases use full immutable Git commit
SHAs. Never embed identity, credentials, client data or a platform repository key
in a release or Packer image. Preserve durable job receipts and reject replay
with a different revision. Keep raw Salt results and pillar out of logs/caches.

Storage changes require verified ownership; never format an unknown filesystem.
Keep Docker/containerd and application data on the verified client block volume.

## Verification

Run these commands from this repository's root:

```sh
python3 -m pip install -r requirements-test.txt ruff
ruff check salt scripts tests
ruff format --check salt scripts tests
python3 -m unittest discover -s tests -v
python3 scripts/validate-salt-minion.py
```

Select checks appropriate to the change; documentation-only edits need no runtime
tests. Run real Salt rendering for state changes. Report source, deployed and
verified state separately.
Do not claim unit tests prove live provisioning.

## Git identity

Use the configured Git identity `litnimax <litnimaxster@gmail.com>` for new commits;
check author and committer with git var before committing. Preserve existing
authorship and do not add an agent co-author.
