{% from 'client_apps/map.jinja' import valid_config with context %}
{% from 'client_agent/map.jinja' import configured, valid_agent with context %}
{% if not valid_config %}
client-apps-start-invalid-pillar:
  test.fail_without_changes:
    - name: Client applications need validated identity, storage, DNS and authentication configuration
{% else %}
include:
  - client_apps.configure
  - client_agent

client-apps-oduflow-validate:
  cmd.run:
    - name: /opt/oduflow/oduflow/1.76.0/tools/oduflow/bin/python /usr/local/libexec/oduflow-validate-oduflow
    - stateful: true
    - require:
      - cmd: client-apps-install-oduflow
      - file: oduflow-config
      - file: client-apps-oduflow-validate-helper

{% for name in ['oduflow', 'paseo', 'paseo-proxy.socket'] %}
client-apps-unmask-{{ name }}:
  service.unmasked:
    - name: {{ name }}
{% endfor %}

client-apps-paseo-running:
  service.running:
    - name: paseo
    - enable: true
    - require:
      - service: client-apps-unmask-paseo
      - cmd: client-apps-install-paseo
      - cmd: paseo-systemd-reload
      - cmd: oduflow-storage-verify
      - file: paseo-home
      - cmd: client-agent-mcp-config
      - file: oduflow-paseo-storage-guard
{% if configured %}
{% if valid_agent %}
      - file: client-agent-config
      - file: client-agent-binary
{% else %}
      - test: client-agent-invalid-pillar
{% endif %}
{% endif %}
    - watch:
      - file: paseo-config
      - file: paseo-password
      - file: paseo-unit-paseo.service
{% if configured and valid_agent %}
      - file: client-agent-config
      - file: client-agent-key
      - file: client-agent-binary
{% endif %}

client-apps-paseo-proxy-running:
  service.running:
    - name: paseo-proxy.socket
    - enable: true
    - require:
      - service: client-apps-unmask-paseo-proxy.socket
      - service: client-apps-paseo-running
      - service: client-apps-docker-service
      - cmd: paseo-systemd-reload
    - watch:
      - file: paseo-unit-paseo-proxy.socket
      - file: paseo-unit-paseo-proxy.service

client-apps-oduflow-running:
  service.running:
    - name: oduflow
    - enable: true
    - require:
      - service: client-apps-unmask-oduflow
      - service: client-apps-docker-service
      - service: client-apps-paseo-proxy-running
      - cmd: client-apps-oduflow-validate
      - cmd: oduflow-systemd-reload
      - cmd: oduflow-storage-verify
      - file: oduflow-oduflow-storage-guard
      - file: oduflow-generated-permissions-postgresql.conf
      - file: oduflow-generated-permissions-postgresql-prod.conf
    - watch:
      - file: oduflow-config
      - file: oduflow-systemd
{% endif %}
