# Repository instructions

Use English for documentation, source comments and UI labels. Respond in the
user's chosen language. Keep this repository limited to software that belongs
on a client VM; platform orchestration belongs in oduflow-platform.

Salt is the configuration authority. Releases use full immutable Git commit
SHAs. Never embed identity, credentials, client data or a platform repository key
in a release or Packer image. Preserve durable job receipts and reject replay
with a different revision. Keep raw Salt results and pillar out of logs/caches.

Storage changes require verified ownership; never format an unknown filesystem.
Keep Docker/containerd and application data on the verified client block volume.

Run Ruff and relevant tests with requirements-test.txt installed. Run real Salt
rendering for state changes. Report source, deployed and verified state separately.
Do not claim unit tests prove live provisioning.

Use the configured Git identity `litnimax <litnimaxster@gmail.com>` for new commits;
check author and committer with git var before committing. Preserve existing
authorship and do not add an agent co-author.
