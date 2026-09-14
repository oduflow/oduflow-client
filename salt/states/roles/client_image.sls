# Salt must be installed from the separately configured, signed Salt repository.
{% set version = salt['pillar.get']('versions:salt', '') %}
{% if version and version.startswith('3006.') %}
oduflow-image-packages:
  pkg.installed:
    - pkgs:
      - xfsprogs
      - util-linux
      - python3
      - openssl
      - salt-minion: {{ version | tojson }}

oduflow-image-minion-stopped:
  service.dead:
    - name: salt-minion
    - enable: false
    - require:
      - pkg: oduflow-image-packages
{% else %}
oduflow-image-version-required:
  test.fail_without_changes:
    - name: A complete Salt 3006 package pin is required in versions.salt
{% endif %}
