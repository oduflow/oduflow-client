{% from 'client_apps/map.jinja' import valid_storage, uid, device with context %}
{% if not valid_storage %}
client-apps-docker-invalid-pillar:
  test.fail_without_changes:
    - name: Docker requires the schema1 client identity and verified data volume
{% else %}
include:
  - oduflow.storage

client-apps-docker-preflight-helper:
  file.managed:
    - name: /usr/local/libexec/oduflow-docker-preflight
    - source: salt://client_apps/files/docker-preflight.py
    - mode: '0700'
    - user: root
    - group: root
    - makedirs: true

client-apps-docker-preflight:
  cmd.run:
    - name: /usr/local/libexec/oduflow-docker-preflight
    - stateful: true
    - require:
      - cmd: oduflow-storage-verify
      - file: client-apps-docker-preflight-helper

client-apps-docker-data-root:
  file.managed:
    - name: /etc/docker/daemon.json
    - user: root
    - group: root
    - mode: '0644'
    - makedirs: true
    - contents: '{"data-root":"/srv/oduflow/data/docker","bip":"172.17.0.1/16","live-restore":false,"log-driver":"local"}'
    - require:
      - cmd: client-apps-docker-preflight

client-apps-containerd-data-root:
  file.managed:
    - name: /etc/containerd/config.toml
    - user: root
    - group: root
    - mode: '0644'
    - makedirs: true
    - contents: |
        version = 2
        root = "/srv/oduflow/data/containerd"
        state = "/run/containerd"
        disabled_plugins = ["io.containerd.grpc.v1.cri", "io.containerd.cri.v1.images", "io.containerd.cri.v1.runtime"]
    - require:
      - cmd: client-apps-docker-preflight

{% for service in ['docker', 'containerd'] %}
client-apps-{{ service }}-guard:
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
        ExecStartPre=/usr/local/libexec/oduflow-storage --instance-uuid {{ uid }} --device {{ device }} --verify --timeout 0
    - require:
      - cmd: client-apps-docker-preflight
{% endfor %}

client-apps-docker-reload:
  cmd.run:
    - name: systemctl daemon-reload
    - onchanges:
      - file: client-apps-docker-guard
      - file: client-apps-containerd-guard

# Config and mount guards precede apt's service auto-start hooks.
client-apps-docker-package:
  pkg.installed:
    - name: docker.io
    - require:
      - file: client-apps-docker-data-root
      - file: client-apps-containerd-data-root
      - cmd: client-apps-docker-reload

{% for service in ['containerd', 'docker'] %}
client-apps-{{ service }}-unmask:
  service.unmasked:
    - name: {{ service }}.service
    - require:
      - pkg: client-apps-docker-package
      - cmd: oduflow-storage-verify
      - cmd: client-apps-docker-reload

client-apps-{{ service }}-service:
  service.running:
    - name: {{ service }}
    - enable: true
    - require:
      - pkg: client-apps-docker-package
      - cmd: oduflow-storage-verify
      - service: client-apps-{{ service }}-unmask
    - watch:
      - file: client-apps-{{ service }}-data-root
{% endfor %}

client-apps-docker-socket-unmask:
  service.unmasked:
    - name: docker.socket
    - require:
      - pkg: client-apps-docker-package
      - cmd: oduflow-storage-verify
      - cmd: client-apps-docker-reload
    - require_in:
      - service: client-apps-docker-service
{% endif %}
