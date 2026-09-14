#!/usr/bin/env bash
# Run from a verified release checkout; enrollment inputs are private files.
set -euo pipefail
umask 077
client_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ $# == 4 ]] || { echo 'Usage: bootstrap.sh UUID MASTER FINGERPRINT INPUT_DIRECTORY' >&2; exit 2; }
instance_uuid=$1
master_ip=$2
master_fingerprint=$3
input_directory=$4
bash "$client_root/salt/minion/install.sh"
install -d -m 0700 /var/cache/salt/minion/extmods/modules
for module in oduflow_job oduflow_release; do
    install -m 0600 "$client_root/salt/minion/extmods/modules/$module.py" \
        "/var/cache/salt/minion/extmods/modules/$module.py"
done
systemctl enable --now tailscaled
headscale_url=$(cat "$input_directory/headscale.url")
tailscale up --login-server="$headscale_url" \
    --auth-key="file:$input_directory/headscale.authkey" \
    --accept-dns=false --hostname="client-$instance_uuid"
python3 "$client_root/salt/minion/bootstrap.py" \
    --instance-uuid "$instance_uuid" --master "$master_ip" \
    --master-fingerprint "$master_fingerprint" \
    --private-key "$input_directory/minion.pem" --public-key "$input_directory/minion.pub"
rm -f -- "$input_directory/minion.pem" "$input_directory/minion.pub" \
    "$input_directory/headscale.authkey"
