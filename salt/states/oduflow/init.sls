{% from 'client_apps/map.jinja' import valid_config, settings with context %}
{% if not valid_config %}
oduflow-invalid-pillar:
  test.fail_without_changes:
    - name: Oduflow needs schema1 storage, direct TLS hostnames, ACME email and UI/MCP credentials
{% else %}
include:
  - client_apps.install
  - client_apps.docker

client-apps-oduflow-validate-helper:
  file.managed:
    - name: /usr/local/libexec/oduflow-validate-oduflow
    - source: salt://oduflow/files/validate.py
    - user: root
    - group: root
    - mode: '0700'
    - makedirs: true

oduflow-config:
  file.managed:
    - name: /etc/oduflow/oduflow.toml
    - source: salt://oduflow/files/oduflow.toml.jinja
    - template: jinja
    - user: root
    - group: root
    - mode: '0600'
    - show_changes: false
    - check_cmd: /opt/oduflow/oduflow/1.76.0/tools/oduflow/bin/python /usr/local/libexec/oduflow-validate-oduflow
    - makedirs: true
    - require:
      - cmd: oduflow-storage-verify
      - cmd: client-apps-install-oduflow
      - file: client-apps-oduflow-validate-helper

{% if settings.get('license_key') %}
oduflow-license:
  file.managed:
    - name: /etc/oduflow/license.key
    - contents_pillar: oduflow:license_key
    - user: root
    - group: root
    - mode: '0600'
    - show_changes: false
    - require:
      - file: oduflow-config
{% endif %}

oduflow-systemd:
  file.managed:
    - name: /etc/systemd/system/oduflow.service
    - source: salt://oduflow/files/oduflow.service
    - user: root
    - group: root
    - mode: '0644'

# Repair only non-secret generated database tuning files from an earlier service
# umask. Missing files remain absent so Oduflow can generate its tuned defaults.
{% for filename in ['postgresql.conf', 'postgresql-prod.conf'] %}
oduflow-generated-permissions-{{ filename }}:
  file.managed:
    - name: /etc/oduflow/{{ filename }}
    - user: root
    - group: root
    - mode: '0644'
    - create: false
    - replace: false
    - require:
      - file: oduflow-config
{% endfor %}

oduflow-needrestart:
  file.managed:
    - name: /etc/needrestart/conf.d/oduflow.conf
    - contents: '$nrconf{override_rc}{qr(^oduflow\.service$)} = 0;'
    - user: root
    - group: root
    - mode: '0644'
    - makedirs: true

oduflow-systemd-reload:
  cmd.run:
    - name: systemctl daemon-reload
    - onchanges:
      - file: oduflow-systemd
{% endif %}
