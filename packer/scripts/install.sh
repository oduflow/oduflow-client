#!/usr/bin/env bash
# Run exclusively on the disposable Ubuntu 24.04 Packer builder.
set -euo pipefail
umask 022
source /etc/os-release
[[ "$ID:$VERSION_ID" == ubuntu:24.04 ]] || { echo 'Unexpected builder OS' >&2; exit 1; }
[[ ! -e /etc/oduflow/instance.json && ! -e /etc/oduflow/storage.json ]] || exit 1
[[ ! -e /srv/oduflow/data && ! -e /etc/oduflow/oduflow.toml ]] || exit 1

# The Paseo source build peaks well above steady-state memory, so the builder
# gets temporary swap. This is build scratch space, never an allocation or data
# mount for a client.
[[ ! -e /oduflow-image.swap ]] || exit 1
fallocate -l 4G /oduflow-image.swap
chmod 0600 /oduflow-image.swap
mkswap /oduflow-image.swap >/dev/null
swapon /oduflow-image.swap

# Package scripts must not start Docker on the boot disk before enrollment.
systemctl mask docker.service docker.socket containerd.service
bash /opt/oduflow-image/salt/minion/install.sh
export DEBIAN_FRONTEND=noninteractive
apt-get install -y docker.io
for service in docker containerd; do
  install -d -m 0755 "/etc/systemd/system/${service}.service.d"
  cat > "/etc/systemd/system/${service}.service.d/image-storage.conf" <<'EOF'
[Unit]
ConditionPathIsMountPoint=/srv/oduflow/data
RequiresMountsFor=/srv/oduflow/data
BindsTo=srv-oduflow-data.mount
After=srv-oduflow-data.mount
EOF
done
systemctl daemon-reload
salt-call --local --file-root=/opt/oduflow-image/salt/states --retcode-passthrough \
  state.apply roles.client_image,client_apps.install pillar='{"versions":{"salt":"3006.27"}}'

install -d -m 0755 /etc/oduflow
python3 - <<'PY'
import json, pathlib, subprocess
artifacts=json.loads(pathlib.Path('/opt/oduflow-image/salt/states/client_apps/artifacts.json').read_text())
packages=subprocess.check_output(['dpkg-query','-W','-f=${Package}=${Version}\n','salt-minion','salt-common','tailscale','docker.io','containerd'],text=True).splitlines()
pathlib.Path('/etc/oduflow/image.json').write_text(json.dumps({'contract':'oduflow-image-v1','os':'ubuntu24.04','base_os_id':2284,'minimum_disk_gb':25,'packages':packages,'applications':artifacts},sort_keys=True)+'\n')
releases={'oduflow':artifacts['oduflow']['version'],'paseo':artifacts['paseo']['version']+'+'+artifacts['paseo']['commit'][:12]}
for app,release in releases.items():
    assert pathlib.Path('/opt/oduflow',app,release,'.installed').is_file(), 'Missing application installation receipt'
assert subprocess.check_output(['agent-browser','--version'],text=True).strip() == 'agent-browser '+artifacts['agent_browser']['version']
chrome=pathlib.Path('/opt/oduflow/chrome',artifacts['chrome']['version'],'chrome-linux64/chrome')
assert artifacts['chrome']['version'] in subprocess.check_output([str(chrome),'--version'],text=True)
assert pathlib.Path('/usr/local/share/oduflow/skills/agent-browser/SKILL.md').is_file()
assert not pathlib.Path('/var/cache/oduflow-apps/paseo-src').exists(), 'Paseo build tree left in the image'
for path in ['/var/lib/docker','/var/lib/containerd']:
    root=pathlib.Path(path)
    assert not root.exists() or not any(root.iterdir()), 'Container data exists on the boot disk'
PY
echo 'Golden-image packages installed; services remain unconfigured and Docker masked.'
