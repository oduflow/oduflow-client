{% from 'client_apps/map.jinja' import uid, device with context %}
{% from 'paseo/project-map.jinja' import configured, valid_project, git with context %}
{% if not configured %}
paseo-project-not-configured:
  test.nop:
    - name: Paseo project awaits the verified client repository
{% elif not valid_project %}
paseo-project-invalid-pillar:
  test.fail_without_changes:
    - name: Paseo project needs matching client GitHub credentials and repository identity
{% else %}
include:
  - paseo.packages
  - github_downloads

paseo-project-helper:
  file.managed:
    - name: /usr/local/libexec/oduflow-paseo-project
    - source: salt://paseo/files/project.py
    - mode: '0700'
    - user: root
    - group: root
    - makedirs: true

paseo-project-config:
  file.managed:
    - name: /etc/paseo/project.json
    - user: root
    - group: root
    - mode: '0600'
    - makedirs: true
    - show_changes: false
    - contents: {{ {'instance_uuid': uid, 'device': device, 'repo': git.repo, 'branch': git.branch} | tojson | tojson }}

paseo-project-storage:
  cmd.run:
    - name: /usr/local/libexec/oduflow-paseo-project --check
    - stateful: true
    - output_loglevel: quiet
    - require:
      - file: paseo-project-helper
      - file: paseo-project-config

paseo-github-config-directory:
  file.directory:
    - name: /srv/oduflow/data/paseo/.config/gh
    - user: paseo
    - group: paseo
    - mode: '0700'
    - makedirs: true
    - require:
      - cmd: paseo-project-storage

paseo-github-credentials:
{% if git.get('auth') == 'ssh' %}
  file.absent:
    - name: /srv/oduflow/data/paseo/.config/gh/hosts.yml
{% else %}
  file.managed:
    - name: /srv/oduflow/data/paseo/.config/gh/hosts.yml
    - user: paseo
    - group: paseo
    - mode: '0600'
    - show_changes: false
    - contents: {{ {'github.com': {'oauth_token': git.token, 'user': git.username, 'git_protocol': 'https'}} | tojson | tojson }}
    - require:
      - file: paseo-github-config-directory

{% endif %}

paseo-project-ready:
  cmd.run:
    - name: /usr/local/libexec/oduflow-paseo-project
    - stateful: true
    - timeout: 360
    - output_loglevel: quiet
    - require:
      - cmd: paseo-project-storage
      - pkg: paseo-github-cli
      - file: paseo-github-credentials
      - sls: github_downloads
{% endif %}
