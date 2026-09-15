# Writes authenticated configuration and units, but does not start app services.
include:
  - oduflow
  - paseo
  - client_agent.mcp
  - agent_browser

{% from 'client_apps/map.jinja' import valid_config with context %}
{% if valid_config %}
extend:
  client-agent-mcp-storage:
    cmd.run:
      - require:
        - file: paseo-home
        - cmd: oduflow-storage-verify
{% endif %}
