{% from 'client_apps/map.jinja' import valid_config with context %}
{% set identity = salt['pillar.get']('oduflow:git_identity', {}) %}
{% if not identity %}
client-git-identity-not-configured:
  test.nop:
    - name: Client Git identity awaits company name and email
{% elif not valid_config or identity is not mapping or identity.get('name') is not string or not identity.name.strip() or identity.get('email') is not string or not identity.email.strip() or '\n' in identity.name or '\r' in identity.name or '\n' in identity.email or '\r' in identity.email %}
client-git-identity-invalid:
  test.fail_without_changes:
    - name: Client Git identity requires valid company name and email
{% else %}
include:
  - paseo

{% for user in ['root', 'paseo'] %}
{% for field in ['name', 'email'] %}
client-git-identity-{{ user }}-{{ field }}:
  git.config_set:
    - name: user.{{ field }}
    - value: {{ identity[field] | tojson }}
    - user: {{ user }}
    - global: true
    - require:
      - user: paseo-user
      - pkg: client-apps-prerequisites
{% endfor %}
{% endfor %}
{% endif %}
