{% from 'client_apps/map.jinja' import valid_config with context %}
{% set downloads = salt['pillar.get']('github_downloads', []) %}
{% set check = namespace(valid=valid_config and downloads is sequence and downloads is not string and downloads is not mapping) %}
{% for item in downloads if check.valid %}
{% if item is not mapping or item.get('repository') is not string or (item.repository | regex_match('[a-z0-9][a-z0-9-]{0,38}/[a-z0-9_.-]{1,100}\\Z')) is none or item.get('identity') is not string or (item.identity | regex_match('[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\Z')) is none or item.get('private_key') is not string or not item.private_key.startswith('-----BEGIN OPENSSH PRIVATE KEY-----\n') %}
{% set check.valid = false %}
{% endif %}
{% endfor %}
{% if not downloads %}
github-downloads-not-configured:
  test.nop: []
{% elif not check.valid %}
github-downloads-invalid-pillar:
  test.fail_without_changes:
    - name: Repository downloads require valid client identity, storage and SSH credentials
{% else %}
include:
  - paseo

github-downloads-known-hosts:
  file.managed:
    - name: /etc/ssh/oduflow-github-known-hosts
    - source: salt://github_downloads/files/known_hosts
    - user: root
    - mode: '0644'
    - makedirs: true

github-downloads-ssh-config:
  file.managed:
    - name: /etc/ssh/ssh_config.d/50-oduflow-downloads.conf
    - source: salt://github_downloads/files/ssh_config.jinja
    - template: jinja
    - user: root
    - mode: '0644'
    - makedirs: true

github-downloads-git-config:
  file.managed:
    - name: /etc/oduflow-github-downloads.gitconfig
    - source: salt://github_downloads/files/gitconfig.jinja
    - template: jinja
    - user: root
    - mode: '0644'
    - makedirs: true

github-downloads-git-include:
  cmd.run:
    - name: git config --system --add include.path /etc/oduflow-github-downloads.gitconfig
    - unless: git config --system --get-all include.path /etc/oduflow-github-downloads.gitconfig
    - require:
      - file: github-downloads-git-config
      - pkg: client-apps-prerequisites

github-downloads-parent-directory:
  file.directory:
    - name: /srv/oduflow/data/github-downloads
    - user: root
    - group: root
    - mode: '0711'
    - makedirs: true
    - require:
      - cmd: oduflow-storage-verify

{% for user in ['root', 'paseo'] %}
github-downloads-directory-{{ user }}:
  file.directory:
    - name: /srv/oduflow/data/github-downloads/{{ user }}
    - user: {{ user }}
    - group: {{ user }}
    - mode: '0700'
    - makedirs: true
    - require:
      - cmd: oduflow-storage-verify
      - user: paseo-user
      - file: github-downloads-parent-directory
{% for item in downloads %}
github-downloads-key-{{ user }}-{{ item.identity }}:
  file.managed:
    - name: /srv/oduflow/data/github-downloads/{{ user }}/{{ item.identity }}
    - contents: {{ item.private_key | tojson }}
    - user: {{ user }}
    - group: {{ user }}
    - mode: '0600'
    - show_changes: false
    - require:
      - file: github-downloads-directory-{{ user }}
{% endfor %}
{% endfor %}
{% set git = salt['pillar.get']('oduflow:git', {}) %}
{% if git.get('auth') == 'ssh' and git.get('repo') in downloads | map(attribute='repository') | list %}
github-downloads-retire-helper:
  file.managed:
    - name: /usr/local/libexec/oduflow-retire-github-token
    - source: salt://github_downloads/files/retire-token.py
    - user: root
    - mode: '0700'
    - makedirs: true

github-downloads-retire-token:
  cmd.run:
    - name: /usr/local/libexec/oduflow-retire-github-token
    - stateful: true
    - output_loglevel: quiet
    - require:
      - cmd: oduflow-storage-verify
      - file: github-downloads-retire-helper
      - file: github-downloads-ssh-config
      - cmd: github-downloads-git-include
{% for item in downloads %}
      - file: github-downloads-key-root-{{ item.identity }}
      - file: github-downloads-key-paseo-{{ item.identity }}
{% endfor %}
{% endif %}
{% endif %}
