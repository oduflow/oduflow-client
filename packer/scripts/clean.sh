#!/usr/bin/env bash
# Final provisioner; the Vultr builder shuts down immediately before snapshotting.
set -euo pipefail
[[ -f /etc/oduflow/image.json ]] || exit 1
[[ ! -e /etc/oduflow/instance.json && ! -e /etc/oduflow/storage.json ]] || exit 1
systemctl mask docker.service docker.socket containerd.service
python3 /opt/oduflow-image/salt/minion/clean-image.py --confirm-disposable-image
systemctl disable tailscaled.service
swapoff /oduflow-image.swap
rm -f /oduflow-image.swap
apt-get clean
rm -rf /opt/oduflow-image /var/cache/salt/minion /root/.cache /root/.npm
rm -f /root/.bash_history /etc/netplan/50-cloud-init.yaml
# Remove the temporary builder key and lock its generated password. Packer keeps
# its existing SSH connection to issue the final graceful shutdown command.
find /root /home -type f \( -name authorized_keys -o -name authorized_keys2 \) -delete
passwd -l root >/dev/null
sync
echo 'Disposable image identity cleanup complete; ready for immediate snapshot shutdown.'
