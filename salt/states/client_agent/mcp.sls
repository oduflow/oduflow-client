{% from 'client_apps/map.jinja' import valid_config, uid, device, dns with context %}
{% if not valid_config %}
client-agent-mcp-invalid-pillar:
  test.fail_without_changes:
    - name: Agent MCP needs verified client identity, storage and endpoint
{% else %}
include:
  - client_agent.cli

# Independent verification permits safe configuration on an existing client.
client-agent-mcp-storage:
  cmd.run:
    - name: /usr/local/libexec/oduflow-storage --instance-uuid {{ uid }} --device {{ device }} --verify --timeout 0
    - stateful: true
    - output_loglevel: quiet

client-agent-mcp-helper:
  file.managed:
    - name: /usr/local/bin/oduflow-agent-mcp
    - source: salt://client_agent/files/mcp.py
    - user: root
    - group: root
    - mode: '0755'
    - makedirs: true

client-agent-mcp-config:
  cmd.run:
    - name: /usr/bin/python3 /usr/local/bin/oduflow-agent-mcp https://{{ dns.oduflow }}/mcp
    - runas: paseo
    - env:
      - HOME: /srv/paseo
    - stateful: true
    - output_loglevel: quiet
    - require:
      - cmd: client-agent-mcp-storage
      - file: client-agent-mcp-helper
      - file: client-agent-codex-binary
      - file: client-agent-claude-binary
{% endif %}
