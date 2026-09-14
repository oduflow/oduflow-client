{% from 'client_agent/map.jinja' import configured, valid_agent, llm, agent_models with context %}
{% if not configured %}
client-agent-not-configured:
  test.nop:
    - name: Coding agent awaits a verified client LiteLLM key and explicit models
{% elif not valid_agent or grains.get('os') != 'Ubuntu' or grains.get('cpuarch') != 'x86_64' %}
client-agent-invalid-pillar:
  test.fail_without_changes:
    - name: Coding agent needs validated Ubuntu amd64 client identity and a complete LiteLLM configuration
{% else %}
include:
  - paseo

# Match the OpenCode SDK generation the pinned Paseo source depends on.
# Official baseline binary works without requiring the host CPU's AVX2 support.
client-agent-package:
  archive.extracted:
    - name: /opt/oduflow/opencode/1.14.46
    - source: https://registry.npmjs.org/opencode-linux-x64-baseline/-/opencode-linux-x64-baseline-1.14.46.tgz
    - source_hash: sha512=a2ae8fc7ed1ea42c24da7667e781140edc5c7066fd2654a7175b2d026e2829478a83f1bbea11846c2820e31dd336222ba40f109d2f60ad1961d68158225fde7e
    - user: root
    - group: root
    - enforce_toplevel: true
    - require:
      - pkg: client-apps-prerequisites

client-agent-binary:
  file.symlink:
    - name: /usr/local/bin/opencode
    - target: /opt/oduflow/opencode/1.14.46/package/bin/opencode
    - force: false
    - require:
      - archive: client-agent-package

client-agent-config-directory:
  file.directory:
    - name: /srv/oduflow/data/paseo/.config/opencode
    - user: paseo
    - group: paseo
    - mode: '0700'
    - makedirs: true
    - require:
      - file: paseo-home
      - cmd: oduflow-storage-verify

client-agent-key:
  file.managed:
    - name: /srv/oduflow/data/paseo/.config/opencode/litellm.key
    - user: paseo
    - group: paseo
    - mode: '0600'
    - show_changes: false
    - contents: {{ llm.key | tojson }}
    - require:
      - file: client-agent-config-directory

client-agent-config:
  file.managed:
    - name: /srv/oduflow/data/paseo/.config/opencode/opencode.json
    - source: salt://client_agent/files/opencode.json.jinja
    - template: jinja
    - user: paseo
    - group: paseo
    - mode: '0600'
    - show_changes: false
    - require:
      - file: client-agent-key
      - file: client-agent-binary
{% endif %}
