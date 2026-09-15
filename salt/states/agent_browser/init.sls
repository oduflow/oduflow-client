{% from 'client_apps/map.jinja' import valid_config with context %}
{% if not valid_config %}
agent-browser-invalid-pillar:
  test.fail_without_changes:
    - name: Browser skills need verified client identity and storage
{% else %}
include:
  - paseo
  - agent_browser.install

{% for agent, directory in [('opencode', '.config/opencode'), ('claude', '.claude'), ('codex', '.agents')] %}
agent-browser-skills-directory-{{ agent }}:
  file.directory:
    - name: /srv/oduflow/data/paseo/{{ directory }}/skills
    - user: paseo
    - group: paseo
    - mode: '0700'
    - makedirs: true
    - require:
      - file: paseo-home
      - cmd: oduflow-storage-verify

agent-browser-skill-{{ agent }}:
  file.symlink:
    - name: /srv/oduflow/data/paseo/{{ directory }}/skills/agent-browser
    - target: /usr/local/share/oduflow/skills/agent-browser
    - user: paseo
    - group: paseo
    - force: false
    - require:
      - file: agent-browser-skills-directory-{{ agent }}
      - file: agent-browser-skill
      - file: agent-browser-command
{% endfor %}
{% endif %}
