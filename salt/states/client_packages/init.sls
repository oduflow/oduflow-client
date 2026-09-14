# Salt owns service restarts; needrestart must not kill its active state worker.
client-packages-restart-policy:
  file.managed:
    - name: /etc/needrestart/conf.d/oduflow-salt.conf
    - contents: "$nrconf{restart} = 'l';"
    - user: root
    - group: root
    - mode: '0644'
    - makedirs: true
    - order: 1
