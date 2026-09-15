# Authenticated application bootstrap only; production creation and public
# readiness are separate controlled steps after DNS/firewall verification.
include:
  - client_receipts
  - client_transport
  - client_apps.health
  - client_ssh
  - github_downloads
  - git_identity
