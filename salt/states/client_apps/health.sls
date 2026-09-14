# Application readiness is part of configure completion. Public TLS and actual
# production-stack readiness remain separate verification steps.
include:
  - client_apps.start
  - paseo.project

client-apps-health-helper:
  file.managed:
    - name: /usr/local/libexec/oduflow-client-health
    - source: salt://client_apps/files/health.py
    - user: root
    - group: root
    - mode: '0700'
    - makedirs: true

client-apps-health:
  cmd.run:
    - name: /usr/local/libexec/oduflow-client-health
    - stateful: true
    - timeout: 250
    - require:
      - file: client-apps-health-helper
      - service: client-apps-oduflow-running
      - service: client-apps-paseo-running
      - file: oduflow-config
      - file: paseo-password

{% from 'paseo/project-map.jinja' import configured, valid_project with context %}
{% if configured %}
extend:
  client-apps-health:
    cmd.run:
      - require:
{% if valid_project %}
        - cmd: paseo-project-ready
  paseo-project-storage:
    cmd.run:
      - require:
        - service: client-apps-paseo-running
{% else %}
        - test: paseo-project-invalid-pillar
{% endif %}
{% endif %}
