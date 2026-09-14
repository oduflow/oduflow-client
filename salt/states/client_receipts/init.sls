{% for module in ["oduflow_job", "oduflow_release"] %}
# Also delivered by cloud-init before the first managed state execution.
# Existing clients receive this maintenance state before protocol activation.
client-receipts-{{ module }}:
  file.managed:
    - name: /var/cache/salt/minion/extmods/modules/{{ module }}.py
    - source: salt://modules/{{ module }}.py
    - user: root
    - group: root
    - mode: '0600'
    - makedirs: true
    - show_changes: false

{% endfor %}
client-receipts-refresh:
  module.run:
    - name: saltutil.refresh_modules
    - onchanges:
      - file: client-receipts-oduflow_job
      - file: client-receipts-oduflow_release
