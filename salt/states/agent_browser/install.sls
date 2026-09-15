# Credential-free packages shared by fresh clients and the Packer image.
{% import_json 'client_apps/artifacts.json' as artifacts %}
{% if grains.get('os') != 'Ubuntu' or grains.get('cpuarch') != 'x86_64' %}
agent-browser-unsupported:
  test.fail_without_changes:
    - name: Agent Browser currently supports Ubuntu amd64 only
{% else %}
agent-browser-dependencies:
  pkg.installed:
    - pkgs:
      - ca-certificates
      - unzip
      - libnss3
      - libatk-bridge2.0-0t64
      - libgtk-3-0t64
      - libasound2t64
      - libgbm1
      - libx11-xcb1
      - libxcomposite1
      - libxdamage1
      - libxfixes3
      - libxrandr2
      - libcups2t64
      - libdrm2
      - libxkbcommon0
      - fonts-liberation

{% for app in ['agent_browser', 'chrome'] %}
agent-browser-package-{{ app }}:
  archive.extracted:
    - name: /opt/oduflow/{{ app }}/{{ artifacts[app].version }}
    - source: {{ artifacts[app].url | tojson }}
    - source_hash: {{ artifacts[app].hash | tojson }}
    - user: root
    - group: root
    - enforce_toplevel: true
    - require:
      - pkg: agent-browser-dependencies
{% endfor %}

# npm's archive ships the native binary without its executable bit.
agent-browser-native-executable:
  file.managed:
    - name: /opt/oduflow/agent_browser/{{ artifacts.agent_browser.version }}/package/bin/agent-browser-linux-x64
    - replace: false
    - mode: '0755'
    - user: root
    - group: root
    - require:
      - archive: agent-browser-package-agent_browser

agent-browser-command:
  file.managed:
    - name: /usr/local/bin/agent-browser
    - source: salt://agent_browser/files/agent-browser.sh.jinja
    - template: jinja
    - user: root
    - group: root
    - mode: '0755'
    - require:
      - file: agent-browser-native-executable
      - archive: agent-browser-package-chrome

agent-browser-skill:
  file.managed:
    - name: /usr/local/share/oduflow/skills/agent-browser/SKILL.md
    - source: salt://agent_browser/files/skill/SKILL.md
    - user: root
    - group: root
    - mode: '0644'
    - makedirs: true

agent-browser-cleanup:
  file.managed:
    - name: /usr/local/libexec/oduflow-browser-cleanup
    - source: salt://agent_browser/files/cleanup.py
    - user: root
    - group: root
    - mode: '0755'
    - makedirs: true
{% endif %}
