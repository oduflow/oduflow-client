# Credential-free installation, also included in the client image build.
{% if grains.get('os') != 'Ubuntu' or grains.get('cpuarch') != 'x86_64' %}
client-agent-clis-unsupported:
  test.fail_without_changes:
    - name: Coding CLIs currently support Ubuntu amd64 only
{% else %}

client-agent-codex-package:
  archive.extracted:
    - name: /opt/oduflow/codex/0.154.0
    - source: https://registry.npmjs.org/@openai/codex/-/codex-0.154.0-linux-x64.tgz
    - source_hash: sha512=6b8148dc0f2c1adc06aceaa5b6b3dbad2da16a3ac7406e7dd44c2645f891a0b31bd74571741b54196e20bba20955810d898180ee4dcfe239511c4a02654fecf5
    - user: root
    - group: root
    - enforce_toplevel: true

client-agent-codex-binary:
  file.symlink:
    - name: /usr/local/bin/codex
    - target: /opt/oduflow/codex/0.154.0/package/vendor/x86_64-unknown-linux-musl/bin/codex
    - force: false
    - require:
      - archive: client-agent-codex-package

client-agent-claude-package:
  archive.extracted:
    - name: /opt/oduflow/claude/2.1.270
    - source: https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64/-/claude-code-linux-x64-2.1.270.tgz
    - source_hash: sha512=0c5c0635ae5e499e3452da09e939720d0ca6d6375492bfbf601ec1d0237499a08fc33c491ef206ea01cca267504708ff05df174327a608c209540b1c2a3961cd
    - user: root
    - group: root
    - enforce_toplevel: true

client-agent-claude-binary:
  file.symlink:
    - name: /usr/local/bin/claude
    - target: /opt/oduflow/claude/2.1.270/package/claude
    - force: false
    - require:
      - archive: client-agent-claude-package
{% endif %}
