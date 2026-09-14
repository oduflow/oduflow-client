#!/usr/bin/env python3
"""Configure VPN-only certificate SSH; print only reduced, public identity facts."""

import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CONFIG = Path("/etc/oduflow/ssh.json")
DROPIN = Path("/etc/ssh/sshd_config.d/00-oduflow-ssh.conf")
NFT = Path("/etc/oduflow/ssh-firewall.nft")


def run(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=20, **kwargs)
    if result.returncode:
        raise ValueError("SSH configuration command failed")
    return result.stdout.strip()


def settings():
    config = json.loads(CONFIG.read_text())
    identity = json.loads(Path("/etc/oduflow/instance.json").read_text())
    if config["instance_uuid"] != identity["instance_uuid"]:
        raise ValueError("SSH instance identity mismatch")
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+=*", config["ca_public_key"]):
        raise ValueError("Invalid SSH CA")
    address = run(["tailscale", "ip", "-4"])
    if ipaddress.ip_address(address) not in ipaddress.ip_network("100.64.0.0/10"):
        raise ValueError("Invalid SSH VPN address")
    return config, address


def configuration(address):
    return f"""# Managed by Oduflow Salt. Provider console recovery is independent of SSH.
Port 22
ListenAddress {address}
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AuthenticationMethods publickey
PermitRootLogin prohibit-password
AuthorizedKeysFile none
AuthorizedKeysCommand none
TrustedUserCAKeys /etc/ssh/oduflow_ca.pub
AuthorizedPrincipalsFile /etc/ssh/oduflow_principals/%u
AllowUsers root
DisableForwarding yes
PermitTunnel no
X11Forwarding no
PermitUserRC no
LogLevel VERBOSE
"""


def firewall():
    return """table inet oduflow_ssh {
 chain ingress {
  type filter hook prerouting priority -310; policy accept;
  iifname != "tailscale0" tcp dport 22 drop
 }
}
"""


def write(path, value, mode=0o644):
    if path.is_symlink():
        raise ValueError("SSH configuration path is a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    changed = not path.exists() or path.read_text() != value
    if changed:
        temporary = path.with_name(path.name + ".new")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
        os.replace(temporary, path)
    os.chmod(path, mode)
    return changed


def effective(address):
    values = run(["/usr/sbin/sshd", "-T", "-C", "user=root,host=localhost,addr=" + address])
    rows = values.splitlines()
    required = [
        "passwordauthentication no",
        "pubkeyauthentication yes",
        "authenticationmethods publickey",
        "permitrootlogin without-password",
        "permituserrc no",
        "permittunnel no",
        "x11forwarding no",
        "kbdinteractiveauthentication no",
        "authorizedkeysfile none",
        "authorizedkeyscommand none",
        "trustedusercakeys /etc/ssh/oduflow_ca.pub",
        "authorizedprincipalsfile /etc/ssh/oduflow_principals/%u",
        "disableforwarding yes",
        "allowusers root",
    ]
    if any(r not in rows for r in required):
        raise ValueError("Conflicting effective SSH configuration")
    listeners = [r for r in rows if r.startswith("listenaddress ")]
    if listeners != ["listenaddress " + address + ":22"]:
        raise ValueError("SSH must listen only on its VPN address")


def apply():
    config, address = settings()
    changed = write(Path("/etc/ssh/oduflow_ca.pub"), config["ca_public_key"] + "\n")
    changed |= write(
        Path("/etc/ssh/oduflow_principals/root"), "instance:" + config["instance_uuid"] + ":admin\n"
    )
    changed |= write(DROPIN, configuration(address))
    Path("/run/sshd").mkdir(exist_ok=True)
    changed |= write(
        Path("/etc/systemd/system/ssh.service.d/oduflow.conf"),
        "[Unit]\nAfter=tailscaled.service\nStartLimitIntervalSec=0\n"
        "[Service]\nRestart=on-failure\nRestartSec=5\n",
    )
    run(["systemctl", "daemon-reload"])
    run(["/usr/sbin/sshd", "-t"])
    effective(address)
    changed |= write(NFT, firewall())
    apply_firewall()
    socket = subprocess.run(["systemctl", "is-active", "--quiet", "ssh.socket"]).returncode == 0
    run(["systemctl", "disable", "--now", "ssh.socket"])
    run(["systemctl", "enable", "ssh.service"])
    if changed or socket:
        run(["systemctl", "restart", "ssh.service"])
    else:
        run(["systemctl", "start", "ssh.service"])
    return {"changed": changed or socket, "comment": "VPN-only certificate SSH configured"}


def apply_firewall():
    existing = (
        subprocess.run(
            ["nft", "list", "table", "inet", "oduflow_ssh"], capture_output=True
        ).returncode
        == 0
    )
    transaction = ("delete table inet oduflow_ssh\n" if existing else "") + NFT.read_text()
    run(["nft", "-f", "-"], input=transaction)


def identity():
    config, address = settings()
    effective(address)
    run(["systemctl", "is-active", "ssh.service"])
    run(["nft", "list", "table", "inet", "oduflow_ssh"])
    key = " ".join(Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text().split()[:2])
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+=*", key):
        raise ValueError("Invalid SSH host key")
    return dict(
        minion_id="client-" + config["instance_uuid"],
        host_key=key,
        vpn_ip=address,
        ca_public_key=config["ca_public_key"],
        ssh_ready=True,
    )


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["identity"]:
            result = identity()
        elif sys.argv[1:] == ["firewall"]:
            apply_firewall()
            result = {"changed": True}
        elif not sys.argv[1:]:
            result = apply()
        else:
            raise ValueError("Invalid SSH operation")
        print(json.dumps(result))
    except Exception:
        print(json.dumps({"changed": False, "error": "ssh_configuration_failed"}))
        sys.exit(1)
