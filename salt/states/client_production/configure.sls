{% from 'client_production/map.jinja' import valid_production, git with context %}
{% from 'client_apps/map.jinja' import uid, dns, settings with context %}
{% if not valid_production %}
client-production-invalid-pillar:
  test.fail_without_changes:
    - name: Production requires complete GitHub credentials, a verified branch, DNS and a generated admin password
{% else %}
include:
  - client_apps.start
  - github_downloads

client-production-helper:
  file.managed:
    - name: /usr/local/libexec/oduflow-create-production
    - source: salt://client_production/files/create-production.py
    - user: root
    - group: root
    - mode: '0700'
    - makedirs: true

client-production-directory:
  file.directory:
    - name: /etc/oduflow
    - user: root
    - group: root
    - mode: '0700'
    - makedirs: true

client-production-config:
  file.managed:
    - name: /etc/oduflow/production.json
    - user: root
    - group: root
    - mode: '0600'
    - show_changes: false
    - contents: {{ {'instance_uuid': uid, 'name': 'main', 'domain': dns.production, 'repo_url': 'https://github.com/' ~ git.repo ~ '.git', 'branch': git.branch, 'team_id': '1', 'team_hostname': dns.oduflow, 'api_host': '172.17.0.1', 'api_port': 8000, 'ui_password_file': '/etc/oduflow/production-ui-password', 'admin_password_file': '/etc/oduflow/production-admin-password', 'git_username': git.username, 'git_token_file': none if git.get('auth') == 'ssh' else '/etc/oduflow/production-git-token'} | tojson | tojson }}
    - require:
      - file: client-production-directory

{% for name, key in [('ui-password', 'ui_password'), ('admin-password', 'production_admin_password'), ('git-token', 'git:token')] if name != 'git-token' or git.get('auth') != 'ssh' %}
client-production-{{ name }}:
  file.managed:
    - name: /etc/oduflow/production-{{ name }}
    - contents_pillar: oduflow:{{ key }}
    - user: root
    - group: root
    - mode: '0600'
    - show_changes: false
    - require:
      - file: client-production-directory
{% endfor %}

{% if git.get('auth') == 'ssh' %}
client-production-git-token:
  file.absent:
    - name: /etc/oduflow/production-git-token
{% endif %}
{% endif %}
