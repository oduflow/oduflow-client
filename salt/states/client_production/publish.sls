{% from 'client_production/map.jinja' import valid_production with context %}
{% from 'client_apps/map.jinja' import uid with context %}
{% set network = salt['pillar.get']('network', {}) %}
{% if not valid_production or network is not mapping or network.get('public_ipv4') is not string or (network.public_ipv4 | regex_match('[0-9]{1,3}(?:\\.[0-9]{1,3}){3}\\Z')) is none %}
client-production-publication-invalid:
  test.fail_without_changes:
    - name: Production publication requires the complete production pillar and verified public IPv4
{% else %}
include:
  - client_production.configure
  - client_apps.health

client-production-verifier-dependency:
  pkg.installed:
    - name: python3-requests

{% for suffix, source in [('publication-guard', 'publication-guard.py'), ('verify-production', 'verify-production.py'), ('publish-production', 'publish.py')] %}
client-production-{{ suffix }}-helper:
  file.managed:
    - name: /usr/local/libexec/oduflow-{{ suffix }}
    - source: salt://client_production/files/{{ source }}
    - user: root
    - group: root
    - mode: '0700'
    - makedirs: true
{% endfor %}

client-production-publication-config:
  file.managed:
    - name: /etc/oduflow/publication.json
    - user: root
    - group: root
    - mode: '0600'
    - show_changes: false
    - contents: {{ {'instance_uuid': uid, 'public_ipv4': network.public_ipv4} | tojson | tojson }}
    - require:
      - file: client-production-directory

# The temporary raw table rules must survive a reboot until hardening completes.
# This service is a prerequisite of Docker, but installing it does not restart Docker.
client-production-boot-guard-unit:
  file.managed:
    - name: /etc/systemd/system/oduflow-production-ingress-guard.service
    - user: root
    - group: root
    - mode: '0644'
    - contents: |
        [Unit]
        Description=Restore unfinished production ingress guard before Docker
        Wants=network-online.target
        After=network-online.target
        Before=docker.service
        [Service]
        Type=oneshot
        ExecStart=/usr/local/libexec/oduflow-publish-production --boot-guard
        RemainAfterExit=yes
    - require:
      - file: client-production-publication-guard-helper
      - file: client-production-publish-production-helper
      - file: client-production-helper
      - file: client-production-directory

client-production-docker-boot-guard:
  file.managed:
    - name: /etc/systemd/system/docker.service.d/oduflow-production-guard.conf
    - user: root
    - group: root
    - mode: '0644'
    - makedirs: true
    - contents: |
        [Unit]
        Requires=oduflow-production-ingress-guard.service
        After=oduflow-production-ingress-guard.service
    - require:
      - file: client-production-boot-guard-unit

client-production-systemd-reload:
  cmd.run:
    - name: systemctl daemon-reload
    - onchanges:
      - file: client-production-boot-guard-unit
      - file: client-production-docker-boot-guard

client-production-publish:
  cmd.run:
    - name: /usr/local/libexec/oduflow-publish-production
    - stateful: true
    - timeout: 1800
    - output_loglevel: quiet
    - require:
      - pkg: client-production-verifier-dependency
      - cmd: client-apps-health
      - cmd: client-production-systemd-reload
      - cmd: oduflow-storage-verify
      - file: client-production-config
      - file: client-production-publication-config
      - file: client-production-ui-password
      - file: client-production-admin-password
      - file: client-production-git-token
      - file: client-production-helper
      - file: client-production-publication-guard-helper
      - file: client-production-verify-production-helper
      - file: client-production-publish-production-helper
{% endif %}
