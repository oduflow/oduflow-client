{% from 'client_apps/map.jinja' import valid_config, uid, dns, settings, paseo with context %}
{% set rotation = salt['pillar.get']('credentials', {}) %}
{% set device = salt['pillar.get']('storage:device', '') %}
{% set uuid_pattern = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\Z' %}
{% set valid = namespace(ok=valid_config and rotation is mapping and rotation.get('request_id') is string and (rotation.request_id | regex_match(uuid_pattern)) is not none and rotation.get('revision') is integer and rotation.revision is not boolean and rotation.revision >= 1 and dns.get('production') is string) %}
{% if valid.ok %}
{% for secret in [settings.get('ui_password'), settings.get('auth_token'), settings.get('production_admin_password'), paseo.get('password')] %}
{% if secret is not string or (secret | regex_match('[A-Za-z0-9_-]{24,256}\\Z')) is none %}
{% set valid.ok = false %}
{% endif %}
{% endfor %}
{% endif %}
{% if not valid.ok %}
client-credentials-invalid:
  test.fail_without_changes:
    - name: Credential rotation requires a revisioned request and complete owned client configuration
{% else %}
client-credentials-existing-production:
  file.exists:
    - name: /var/lib/oduflow/production/receipt.json

client-credentials-existing-storage:
  file.exists:
    - name: /etc/oduflow/storage.json

client-credentials-helper:
  file.managed:
    - name: /usr/local/libexec/oduflow-rotate-credentials
    - source: salt://client_credentials/files/rotate.py
    - user: root
    - group: root
    - mode: '0700'
    - makedirs: true

client-credentials-config:
  file.managed:
    - name: /etc/oduflow/credentials-desired.json
    - user: root
    - group: root
    - mode: '0600'
    - show_changes: false
    - contents: {{ {'instance_uuid': uid, 'request_id': rotation.request_id, 'revision': rotation.revision, 'device': device, 'oduflow_hostname': dns.oduflow, 'paseo_hostname': dns.paseo, 'production_domain': dns.production, 'ui_password': settings.ui_password, 'auth_token': settings.auth_token, 'paseo_password': paseo.password, 'production_admin_password': settings.production_admin_password} | tojson | tojson }}
    - require:
      - file: client-credentials-existing-storage
      - file: client-credentials-existing-production

client-credentials-apply:
  cmd.run:
    - name: /usr/local/libexec/oduflow-rotate-credentials
    - stateful: true
    - timeout: 900
    - require:
      - file: client-credentials-helper
      - file: client-credentials-config
{% endif %}
