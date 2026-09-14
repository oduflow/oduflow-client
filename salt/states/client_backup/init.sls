{% set backup = salt['pillar.get']('backup', {}) %}
{% set uid = salt['pillar.get']('instance_uuid', '') %}
{% set uuid_pattern = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\Z' %}
{% set endpoint_pattern = 'https://[a-f0-9]{32}' ~ ('' if backup.get('jurisdiction') == 'default' else '\\.' ~ backup.get('jurisdiction', 'invalid')) ~ '\\.r2\\.cloudflarestorage\\.com\\Z' %}
{% set valid = salt['pillar.get']('schema', 0) == 1 and uid is string and (uid | regex_match(uuid_pattern)) is not none and grains.get('id') == 'client-' ~ uid and backup is mapping and backup.get('enabled') is sameas true and backup.get('provider') == 'cloudflare_r2' and backup.get('bucket') == 'oduflow-' ~ uid and backup.get('jurisdiction') in ['default', 'eu', 'us'] and backup.get('endpoint') is string and (backup.endpoint | regex_match(endpoint_pattern)) is not none and backup.get('access_key_id') is string and (backup.access_key_id | regex_match('[a-f0-9]{32}\\Z')) is not none and backup.get('secret_access_key') is string and (backup.secret_access_key | regex_match('[a-f0-9]{64}\\Z')) is not none and backup.get('repository_password') is string and backup.repository_password | length >= 32 and backup.get('retention_days') is integer and backup.get('retention_days') is not boolean and backup.retention_days >= 1 and backup.retention_days <= 3650 %}
{% if not valid %}
client-backup-invalid-pillar:
  test.fail_without_changes:
    - name: Client backup requires complete scoped R2 credentials and retention
{% else %}
include:
  - client_packages

client-backup-package:
  pkg.installed:
    - name: restic

client-backup-directory:
  file.directory:
    - name: /etc/oduflow
    - user: root
    - group: root
    - mode: '0700'

client-backup-helper:
  file.managed:
    - name: /usr/local/libexec/oduflow-backup
    - source: salt://client_backup/files/backup.py
    - user: root
    - group: root
    - mode: '0700'
    - makedirs: true

client-backup-environment:
  file.managed:
    - name: /etc/oduflow/backup.env
    - user: root
    - group: root
    - mode: '0600'
    - show_changes: false
    - contents: |
        RESTIC_REPOSITORY={{ ('s3:' ~ backup.endpoint ~ '/' ~ backup.bucket) | tojson }}
        RESTIC_PASSWORD={{ backup.repository_password | tojson }}
        AWS_ACCESS_KEY_ID={{ backup.access_key_id | tojson }}
        AWS_SECRET_ACCESS_KEY={{ backup.secret_access_key | tojson }}
        RESTIC_CACHE_DIR=/srv/oduflow/data/.oduflow-backup/cache
        ODUFLOW_INSTANCE_UUID={{ uid | tojson }}
        ODUFLOW_BACKUP_RETENTION_DAYS={{ backup.retention_days }}
    - require:
      - file: client-backup-directory

client-backup-service:
  file.managed:
    - name: /etc/systemd/system/oduflow-backup.service
    - user: root
    - group: root
    - mode: '0644'
    - contents: |
        [Unit]
        Description=Back up the stopped Oduflow client data volume to R2
        Wants=network-online.target
        After=network-online.target srv-oduflow-data.mount
        Requires=srv-oduflow-data.mount
        [Service]
        Type=oneshot
        EnvironmentFile=/etc/oduflow/backup.env
        ExecStart=/usr/local/libexec/oduflow-backup
        Nice=10
        IOSchedulingClass=best-effort
        IOSchedulingPriority=7
        PrivateTmp=true
        NoNewPrivileges=true
    - require:
      - file: client-backup-helper
      - file: client-backup-environment

client-backup-timer:
  file.managed:
    - name: /etc/systemd/system/oduflow-backup.timer
    - user: root
    - group: root
    - mode: '0644'
    - contents: |
        [Unit]
        Description=Daily Oduflow client data backup
        [Timer]
        OnCalendar=*-*-* 02:00:00 UTC
        RandomizedDelaySec=3600
        Persistent=true
        Unit=oduflow-backup.service
        [Install]
        WantedBy=timers.target
    - require:
      - file: client-backup-service

client-backup-systemd-reload:
  cmd.run:
    - name: systemctl daemon-reload
    - onchanges:
      - file: client-backup-service
      - file: client-backup-timer

client-backup-initial-restore-test:
  cmd.run:
    - name: >-
        /bin/bash -c 'set -a; . /etc/oduflow/backup.env; exec /usr/local/libexec/oduflow-backup --verify-restore'
    - stateful: true
    - timeout: 7200
    - output_loglevel: quiet
    - require:
      - pkg: client-backup-package
      - cmd: client-backup-systemd-reload
      - cmd: client-production-publish
      - cmd: oduflow-storage-verify
      - file: client-backup-environment

client-backup-timer-running:
  service.running:
    - name: oduflow-backup.timer
    - enable: true
    - require:
      - cmd: client-backup-initial-restore-test
      - file: client-backup-timer
{% endif %}
