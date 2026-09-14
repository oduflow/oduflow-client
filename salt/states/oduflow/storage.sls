{% set uid = salt['pillar.get']('instance_uuid', '') %}
{% set device = salt['pillar.get']('storage:device', '') %}
{% set mountpoint = salt['pillar.get']('storage:mount', '') %}
{% set fs_uuid = salt['pillar.get']('storage:filesystem_uuid', '') %}
{% set uuid_pattern = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\Z' %}
{% set valid = salt['pillar.get']('schema', 0) == 1 and uid is string and (uid | regex_match(uuid_pattern)) is not none and grains.get('id') == 'client-' ~ uid and device is string and (device | regex_match('/dev/disk/by-id/[A-Za-z0-9_.:+-]+\\Z')) is not none and mountpoint == '/srv/oduflow/data' and (not fs_uuid or (fs_uuid is string and (fs_uuid | regex_match(uuid_pattern)) is not none)) %}
{% if not valid %}
oduflow-storage-invalid-pillar:
  test.fail_without_changes:
    - name: Missing or inconsistent schema1 instance identity and stable storage path
{% else %}
oduflow-storage-packages:
  pkg.installed:
    - pkgs:
      - xfsprogs
      - util-linux
      - python3

oduflow-storage-helper:
  file.managed:
    - name: /usr/local/libexec/oduflow-storage
    - source: salt://oduflow/files/storage.py
    - user: root
    - group: root
    - mode: '0700'
    - makedirs: true

oduflow-storage-prepare:
  cmd.run:
    - name: >-
        /usr/local/libexec/oduflow-storage --instance-uuid {{ uid | tojson }} --device {{ device | tojson }}{% if fs_uuid %} --filesystem-uuid {{ fs_uuid | tojson }}{% endif %}{% if salt['pillar.get']('storage:allow_format', false) is sameas true %} --allow-format{% endif %}
    - stateful: true
    - require:
      - pkg: oduflow-storage-packages
      - file: oduflow-storage-helper

oduflow-storage-mount:
  mount.mounted:
    - name: /srv/oduflow/data
    - device: {{ device | tojson }}
    - fstype: xfs
    - opts: defaults,prjquota
    - mkmnt: true
    - persist: true
    - require:
      - cmd: oduflow-storage-prepare

oduflow-storage-verify:
  cmd.run:
    - name: >-
        /usr/local/libexec/oduflow-storage --instance-uuid {{ uid | tojson }} --device {{ device | tojson }} --verify
    - stateful: true
    - require:
      - mount: oduflow-storage-mount

# Drop-ins must precede future service.running states for these unit names.
{% for service in ['oduflow', 'paseo'] %}
oduflow-{{ service }}-storage-guard:
  file.managed:
    - name: /etc/systemd/system/{{ service }}.service.d/storage.conf
    - user: root
    - group: root
    - mode: '0644'
    - makedirs: true
    - contents: |
        [Unit]
        RequiresMountsFor=/srv/oduflow/data
        BindsTo=srv-oduflow-data.mount
        After=srv-oduflow-data.mount
        [Service]
        ExecStartPre=+/usr/local/libexec/oduflow-storage --instance-uuid {{ uid }} --device {{ device }} --verify --timeout 0
    - require:
      - cmd: oduflow-storage-verify
{% endfor %}

oduflow-storage-systemd-reload:
  cmd.run:
    - name: systemctl daemon-reload
    - onchanges:
      - file: oduflow-oduflow-storage-guard
      - file: oduflow-paseo-storage-guard
{% endif %}
