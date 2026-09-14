# New clients receive this configuration before their first minion start.
# Existing clients need one controlled restart after the current receipt has
# completed. Never restart the minion from inside its own active Salt job.
client-transport-config:
  file.managed:
    - name: /etc/salt/minion.d/oduflow-transport.conf
    - user: root
    - group: root
    - mode: '0600'
    - makedirs: true
    - show_changes: false
    - contents: |
        {
          "tcp_keepalive": true,
          "tcp_keepalive_idle": 30,
          "tcp_keepalive_intvl": 10,
          "tcp_keepalive_cnt": 3,
          "master_alive_interval": 30
        }
