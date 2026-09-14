{% set uid = salt['pillar.get']('instance_uuid', '') %}
{% set device = salt['pillar.get']('storage:device', '') %}
{% set resize = salt['pillar.get']('storage:resize', {}) %}
{% set uuid_pattern = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\Z' %}
{% set valid = salt['pillar.get']('schema', 0) == 1 and uid is string and (uid | regex_match(uuid_pattern)) is not none and grains.get('id') == 'client-' ~ uid and device is string and (device | regex_match('/dev/disk/by-id/[A-Za-z0-9_.:+-]+\\Z')) is not none and salt['pillar.get']('storage:mount') == '/srv/oduflow/data' and resize is mapping and resize.get('request_id') is string and (resize.request_id | regex_match(uuid_pattern)) is not none and resize.get('target_size_gb') is integer and resize.target_size_gb is not boolean and 1 <= resize.target_size_gb <= 10000 %}
{% if not valid %}
client-storage-resize-invalid:
  test.fail_without_changes:
    - name: Online growth requires an authenticated request, existing storage identity and target capacity
{% else %}
client-storage-resize-existing-owner:
  file.exists:
    - name: /etc/oduflow/storage.json

client-storage-resize-verifier:
  file.managed:
    - name: /usr/local/libexec/oduflow-storage
    - source: salt://oduflow/files/storage.py
    - user: root
    - group: root
    - mode: '0700'
    - makedirs: true

client-storage-resize-helper:
  file.managed:
    - name: /usr/local/libexec/oduflow-grow-storage
    - source: salt://client_storage_resize/files/grow.py
    - user: root
    - group: root
    - mode: '0700'
    - makedirs: true

client-storage-resize-config:
  file.managed:
    - name: /etc/oduflow/storage-resize.json
    - user: root
    - group: root
    - mode: '0600'
    - contents: {{ {'instance_uuid': uid, 'device': device, 'request_id': resize.request_id, 'target_size_gb': resize.target_size_gb} | tojson | tojson }}
    - require:
      - file: client-storage-resize-existing-owner

client-storage-resize-grow:
  cmd.run:
    - name: /usr/local/libexec/oduflow-grow-storage
    - stateful: true
    - timeout: 300
    - require:
      - file: client-storage-resize-existing-owner
      - file: client-storage-resize-verifier
      - file: client-storage-resize-helper
      - file: client-storage-resize-config
{% endif %}
