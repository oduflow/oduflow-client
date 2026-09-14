#!/usr/bin/env bash
# Prepare a clean Ubuntu host for explicit, separately authorized enrollment.
set -euo pipefail
umask 022

fail() { echo "Minion prerequisites: $*" >&2; exit 1; }
[[ $(id -u) == 0 ]] || fail 'run as root'
[[ $(uname -m) == x86_64 ]] || fail 'only amd64 is supported'
[[ -r /etc/os-release ]] || fail 'missing operating-system metadata'
# shellcheck source=/dev/null
. /etc/os-release
case "${ID:-}:${VERSION_ID:-}:${VERSION_CODENAME:-}" in
    ubuntu:24.04:noble | ubuntu:26.04:resolute) ;;
    *) fail 'only Ubuntu 24.04 noble and 26.04 resolute are supported' ;;
esac
[[ -d /run/systemd/system ]] || fail 'a running systemd host is required'

# Refuse before package, file, or service changes. Existing enrollment is managed
# through bootstrap.py, never by rerunning a prerequisites installer over its PKI.
for identity in /etc/oduflow/instance.json /etc/salt/minion_id /etc/salt/minion.d/oduflow.conf; do
    [[ ! -e "$identity" && ! -L "$identity" ]] || fail "existing identity: $identity"
done
[[ ! -L /etc/salt/pki/minion ]] || fail 'minion PKI directory is a symlink'
[[ ! -e /etc/salt/pki/minion || -d /etc/salt/pki/minion ]] || fail 'invalid minion PKI path'
if [[ -d /etc/salt/pki/minion ]] &&
    [[ -n $(find /etc/salt/pki/minion -mindepth 1 -print -quit) ]]; then
    fail 'existing minion PKI; refusing to install over an enrolled host'
fi
# Even a state created by an interrupted install needs operator inspection.
# Never reset another VPN identity to make this clean-host preflight pass.
if [[ -L /var/lib/tailscale/tailscaled.state || -s /var/lib/tailscale/tailscaled.state ]]; then
    fail 'existing Tailscale state; inspect its ownership before any enrollment'
fi
if systemctl is-active --quiet salt-minion.service; then
    fail 'existing running salt-minion; refusing to change it'
fi
pinned_packages=0
for package in salt-common salt-minion tailscale; do
    installed=$(dpkg-query -W -f='${db:Status-Status} ${Version}' "$package" 2>/dev/null || true)
    if [[ "$installed" == installed\ * ]]; then
        expected=3006.27
        [[ "$package" != tailscale ]] || expected=1.102.4
        [[ "$installed" == "installed $expected" ]] || fail "unexpected installed $package version"
        pinned_packages=$((pinned_packages + 1))
    fi
done

# A cleaned image already contains these packages. Verify both package metadata
# and the executable versions before skipping every network/package operation.
# The identity preflight above also applies to this path; an image marker alone
# is never evidence that a host is safe to enroll.
verify_binary_versions() {
    local salt_version tailscale_version
    salt_version=$(salt-minion --version) || fail 'Salt executable validation failed'
    tailscale_version=$(tailscale version) || fail 'Tailscale executable validation failed'
    [[ "$salt_version" =~ ^salt-minion\ 3006\.27(\ \([A-Za-z\ ]+\))?$ ]] || fail 'unexpected Salt executable version'
    [[ "${tailscale_version%%$'\n'*}" == 1.102.4 ]] || fail 'unexpected Tailscale executable version'
}
prerequisites_ready=true
for package in ca-certificates curl gnupg python3 openssl xfsprogs util-linux; do
    installed=$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)
    [[ "$installed" == installed ]] || prerequisites_ready=false
done
if [[ "$pinned_packages" == 3 && "$prerequisites_ready" == true ]]; then
    verify_binary_versions
    systemctl mask salt-minion.service
    systemctl stop salt-minion.service
    echo 'Pinned prerequisites verified. Salt minion remains masked; enrollment is separate.'
    exit 0
fi

# Prevent the minion postinst from starting with a default hostname/master.
systemctl mask salt-minion.service
if dpkg-query -W -f='${db:Status-Status}' salt-minion 2>/dev/null | grep -qx installed; then
    systemctl stop salt-minion.service
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gnupg python3 openssl xfsprogs util-linux

download_dir=$(mktemp -d /tmp/oduflow-minion-install.XXXXXXXX)
trap 'rm -rf -- "$download_dir"' EXIT
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 15 --max-time 90 \
    https://packages.broadcom.com/artifactory/api/security/keypair/SaltProjectKey/public \
    -o "$download_dir/salt-public.asc"
printf '%s  %s\n' \
    36decef986477acb8ba2a1fc4041bcf9f22229ef6c939d0317c9e36a9d142b34 \
    "$download_dir/salt-public.asc" | sha256sum --check --status
gpg --batch --yes --dearmor -o "$download_dir/salt-archive-keyring.pgp" \
    "$download_dir/salt-public.asc"
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 15 --max-time 90 \
    "https://pkgs.tailscale.com/stable/ubuntu/${VERSION_CODENAME}.noarmor.gpg" \
    -o "$download_dir/tailscale-archive-keyring.gpg"
printf '%s  %s\n' \
    3e03dacf222698c60b8e2f990b809ca1b3e104de127767864284e6c228f1fb39 \
    "$download_dir/tailscale-archive-keyring.gpg" | sha256sum --check --status

install -d -m 0755 /etc/apt/keyrings /usr/share/keyrings /etc/apt/preferences.d
install -m 0644 "$download_dir/salt-archive-keyring.pgp" /etc/apt/keyrings/salt-archive-keyring.pgp
install -m 0644 "$download_dir/tailscale-archive-keyring.gpg" /usr/share/keyrings/tailscale-archive-keyring.gpg
cat > /etc/apt/sources.list.d/salt.sources <<'EOF'
Types: deb
URIs: https://packages.broadcom.com/artifactory/saltproject-deb
Suites: stable
Components: main
Architectures: amd64
Signed-By: /etc/apt/keyrings/salt-archive-keyring.pgp
EOF
cat > /etc/apt/sources.list.d/tailscale.list <<EOF
deb [arch=amd64 signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/ubuntu ${VERSION_CODENAME} main
EOF
cat > /etc/apt/preferences.d/oduflow-minion <<'EOF'
Package: salt-common salt-minion
Pin: version 3006.27
Pin-Priority: 1001

Package: tailscale
Pin: version 1.102.4
Pin-Priority: 1001
EOF
apt-get update
apt-get install -y salt-common=3006.27 salt-minion=3006.27 tailscale=1.102.4
systemctl stop salt-minion.service
verify_binary_versions
echo 'Prerequisites installed. Salt minion remains masked; VPN and Salt enrollment are separate steps.'
