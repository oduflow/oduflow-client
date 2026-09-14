{% set ssh = salt['pillar.get']('oduflow:ssh', {}) %}
{% if ssh %}
client-ssh-packages:
  pkg.installed:
    - pkgs:
      - openssh-server
      - nftables

client-ssh-config:
  file.managed:
    - name: /etc/oduflow/ssh.json
    - user: root
    - group: root
    - mode: '0600'
    - makedirs: true
    - show_changes: false
    - contents: |
        {{ {'instance_uuid': pillar['instance_uuid'], 'ca_public_key': ssh['ca_public_key']} | json }}

client-ssh-helper:
  file.managed:
    - name: /usr/local/libexec/oduflow-ssh
    - source: salt://client_ssh/files/configure.py
    - user: root
    - group: root
    - mode: '0700'
    - makedirs: true

client-ssh-apply:
  cmd.run:
    - name: /usr/local/libexec/oduflow-ssh
    - stateful: true
    - timeout: 100
    - require:
      - pkg: client-ssh-packages
      - file: client-ssh-config
      - file: client-ssh-helper

client-ssh-firewall-service:
  file.managed:
    - name: /etc/systemd/system/oduflow-ssh-firewall.service
    - mode: '0644'
    - contents: |
        [Unit]
        Description=Oduflow private SSH ingress
        DefaultDependencies=no
        After=local-fs.target
        Before=network-pre.target ssh.service
        Wants=network-pre.target
        [Service]
        Type=oneshot
        ExecStart=/usr/local/libexec/oduflow-ssh firewall
        RemainAfterExit=yes
        [Install]
        WantedBy=multi-user.target

client-ssh-systemd-reload:
  cmd.run:
    - name: systemctl daemon-reload
    - onchanges:
      - file: client-ssh-firewall-service

client-ssh-firewall-running:
  service.running:
    - name: oduflow-ssh-firewall
    - enable: true
    - require:
      - file: client-ssh-firewall-service
      - cmd: client-ssh-systemd-reload
      - cmd: client-ssh-apply
{% endif %}
