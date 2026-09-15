{% from 'client_apps/map.jinja' import valid_config, dns, paseo, settings with context %}
{% if not valid_config %}
paseo-invalid-pillar:
  test.fail_without_changes:
    - name: Paseo needs schema1 storage, direct TLS hostnames and generated authentication credentials
{% else %}
include:
  - client_apps.install
  - client_apps.docker

paseo-user:
  user.present:
    - name: paseo
    - system: true
    - home: /srv/paseo
    - createhome: false
    - shell: /bin/bash

paseo-data:
  file.directory:
    - name: /srv/oduflow/data/paseo
    - user: paseo
    - group: paseo
    - mode: '0700'
    - require:
      - user: paseo-user
      - cmd: oduflow-storage-verify

paseo-home:
  file.symlink:
    - name: /srv/paseo
    - target: /srv/oduflow/data/paseo
    - force: false
    - require:
      - file: paseo-data

paseo-config:
  file.managed:
    - name: /srv/oduflow/data/paseo/config.json
    - user: paseo
    - group: paseo
    - mode: '0600'
    - show_changes: false
    - contents: {{ {'version': 1, 'daemon': {'listen': '127.0.0.1:6767', 'hostnames': ['localhost', '127.0.0.1', dns.paseo], 'trustedProxies': ['127.0.0.1/32'], 'relay': {'enabled': false}}, 'features': {'webUi': {'enabled': true}}, 'agents': {'providers': {'claude': {'enabled': false}, 'codex': {'enabled': false}, 'copilot': {'enabled': false}, 'opencode': {'enabled': true}, 'pi': {'enabled': false}, 'omp': {'enabled': false}}}} | tojson | tojson }}
    - require:
      - file: paseo-home

# systemd reads this as root before switching to the unprivileged service user.
# Password is never a command argument. URL-safe form is checked before render.
paseo-password:
  file.managed:
    - name: /etc/paseo/credentials.env
    - user: root
    - group: root
    - mode: '0600'
    - makedirs: true
    - show_changes: false
    - contents: {{ ('PASEO_PASSWORD=' ~ paseo.password ~ '\nODUFLOW_MCP_TOKEN=' ~ settings.auth_token ~ '\n') | tojson }}

# The unit resolves the built Paseo release and its Node runtime from the same
# pinned manifest the installer uses.
paseo-unit-paseo.service:
  file.managed:
    - name: /etc/systemd/system/paseo.service
    - source: salt://paseo/files/paseo.service.jinja
    - template: jinja
    - user: root
    - group: root
    - mode: '0644'

{% for unit in ['paseo-proxy.socket', 'paseo-proxy.service', 'paseo-nightly-restart.service', 'paseo-nightly-restart.timer'] %}
paseo-unit-{{ unit }}:
  file.managed:
    - name: /etc/systemd/system/{{ unit }}
    - source: salt://paseo/files/{{ unit }}
    - user: root
    - group: root
    - mode: '0644'
{% endfor %}

paseo-systemd-reload:
  cmd.run:
    - name: systemctl daemon-reload
    - onchanges:
      - file: paseo-unit-paseo.service
      - file: paseo-unit-paseo-proxy.socket
      - file: paseo-unit-paseo-proxy.service
      - file: paseo-unit-paseo-nightly-restart.service
      - file: paseo-unit-paseo-nightly-restart.timer
{% endif %}
