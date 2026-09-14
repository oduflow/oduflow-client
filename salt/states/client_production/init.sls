{% include 'client_production/configure.sls' %}
{% from 'client_production/map.jinja' import valid_production with context %}
{% if valid_production %}
# The controller verifies provider ingress is closed before writing this guard.
# A missing guard never triggers creation; the helper also checks its contents,
# ownership and mode. The state never publishes the new application itself.
client-production-create:
  cmd.run:
    - name: /usr/local/libexec/oduflow-create-production
    - stateful: true
    - output_loglevel: quiet
    - onlyif: test -f /etc/oduflow/production-publication-guard.json
    - require:
      - file: client-production-helper
      - file: client-production-config
      - file: client-production-ui-password
      - file: client-production-admin-password
      - file: client-production-git-token
      - service: client-apps-oduflow-running
      - cmd: oduflow-storage-verify
{% endif %}
