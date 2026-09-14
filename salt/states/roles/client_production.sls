# Fixed controller role: prepare, guard, harden, publish, then verify actual HTTPS.
include:
  - client_production.publish
{% if salt['pillar.get']('backup:enabled', False) is sameas true %}
  - client_backup
{% endif %}
