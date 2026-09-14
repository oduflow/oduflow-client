# Package installation only: no service starts and no pillar secrets required.
{% import_json 'client_apps/artifacts.json' as artifacts %}
{% set method = salt['pillar.get']('paseo:install_method', 'source') %}
{% set runtime = salt['pillar.get']('paseo:runtime', {}) %}
{% if method not in ['source', 'prebuilt'] or (method == 'prebuilt' and (runtime is not mapping or runtime.get('source') is not string or runtime.get('hash') is not string or (runtime.hash | regex_match('sha256=[0-9a-f]{64}\\Z')) is none)) %}
client-apps-invalid-install-method:
  test.fail_without_changes:
    - name: Select source or prebuilt with a runtime source and SHA256 checksum
{% elif grains.get('os') != 'Ubuntu' or grains.get('cpuarch') != 'x86_64' %}
client-apps-unsupported-platform:
  test.fail_without_changes:
    - name: Client application packages currently support Ubuntu amd64 only
{% else %}
include:
  - client_packages
  - paseo.packages
  - client_agent.cli

client-apps-prerequisites:
  pkg.installed:
    - pkgs:
      - ca-certificates
      - python3
      - python3-venv
      - git
      - fuse-overlayfs
      - rsync
      - xz-utils
      - build-essential
      - pkg-config

client-apps-cache:
  file.directory:
    - name: /var/cache/oduflow-apps
    - user: root
    - group: root
    - mode: '0755'

{% for app in ['uv', 'node'] %}
client-apps-{{ app }}:
  archive.extracted:
    - name: /opt/oduflow/{{ app }}/{{ artifacts[app].version }}
    - source: {{ artifacts[app].url | tojson }}
    - source_hash: {{ artifacts[app].hash | tojson }}
    - user: root
    - group: root
    - enforce_toplevel: true
    - require:
      - pkg: client-apps-prerequisites
{% endfor %}

client-apps-installer:
  file.managed:
    - name: /usr/local/libexec/oduflow-install-app
    - source: salt://client_apps/files/install.py
    - user: root
    - group: root
    - mode: '0700'
    - makedirs: true

# The installer reads the same pinned manifest the states render from, so a
# version, commit or workspace list is declared exactly once.
client-apps-manifest:
  file.managed:
    - name: /var/cache/oduflow-apps/artifacts.json
    - contents: {{ dict(artifacts, paseo=dict(artifacts.paseo, install_method=method, runtime=runtime)) | tojson | tojson }}
    - user: root
    - group: root
    - mode: '0644'
    - require:
      - file: client-apps-cache

client-apps-oduflow-artifact:
  file.managed:
    - name: /var/cache/oduflow-apps/oduflow-{{ artifacts.oduflow.version }}-py3-none-any.whl
    - source: {{ artifacts.oduflow.url | tojson }}
    - source_hash: {{ artifacts.oduflow.hash | tojson }}
    - user: root
    - group: root
    - mode: '0644'
    - require:
      - file: client-apps-cache

client-apps-install-oduflow:
  cmd.run:
    - name: /usr/local/libexec/oduflow-install-app oduflow
    - stateful: true
    - require:
      - file: client-apps-installer
      - file: client-apps-manifest
      - file: client-apps-oduflow-artifact
      - archive: client-apps-uv
      - archive: client-apps-node

{% if method == 'source' and artifacts.paseo.get('archive') %}
client-apps-paseo-artifact:
  file.managed:
    - name: /var/cache/oduflow-apps/paseo-{{ artifacts.paseo.commit }}.tar.gz
    - source: {{ artifacts.paseo.archive.source | tojson }}
    - source_hash: {{ artifacts.paseo.archive.hash | tojson }}
    - user: root
    - group: root
    - mode: '0600'
    - require:
      - file: client-apps-cache
{% endif %}

{% if method == 'prebuilt' %}
client-apps-paseo-runtime:
  file.managed:
    - name: /var/cache/oduflow-apps/paseo-runtime.tar.gz
    - source: {{ runtime.source | tojson }}
    - source_hash: {{ runtime.hash | tojson }}
    - user: root
    - mode: '0600'
    - require:
      - file: client-apps-cache
{% endif %}

# Paseo is built from the pinned commit of {{ artifacts.paseo.repository }};
# a cold build downloads the monorepo dependency tree and takes far longer than
# a package installation. The golden image performs it once for every client.
client-apps-install-paseo:
  cmd.run:
    - name: /usr/local/libexec/oduflow-install-app paseo
    - stateful: true
    - timeout: 5400
    - require:
      - file: client-apps-installer
      - file: client-apps-manifest
      - archive: client-apps-uv
      - archive: client-apps-node
{% if method == 'source' and artifacts.paseo.get('archive') %}
      - file: client-apps-paseo-artifact
{% endif %}
{% if method == 'prebuilt' %}
      - file: client-apps-paseo-runtime
{% endif %}
{% endif %}
