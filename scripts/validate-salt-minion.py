#!/usr/bin/python3
"""Compile Salt high data locally without executing any state or block operation."""

import importlib.util
import tempfile
from pathlib import Path

import salt.config
import salt.loader
import salt.state
import salt.template
from salt.utils.crypt import pem_finger

ROOT = Path(__file__).resolve().parents[1]
UUID = "12345678-1234-1234-1234-123456789abc"
spec = importlib.util.spec_from_file_location("bootstrap", ROOT / "salt/minion/bootstrap.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)

with tempfile.TemporaryDirectory(prefix="oduflow-salt-compile-") as temporary:
    temp = Path(temporary)
    pem = "-----BEGIN PUBLIC KEY-----\nYWJj\nZGVm\n-----END PUBLIC KEY-----\n"
    public_key = temp / "fixture.pub"
    public_key.write_text(pem)
    assert bootstrap.fingerprint(pem) == pem_finger(path=str(public_key), sum_type="sha256")
    opts = salt.config.minion_config(None)
    opts.update(
        file_client="local",
        cachedir=str(temp / "cache"),
        pki_dir=str(temp / "pki"),
        sock_dir=str(temp / "sock"),
        extension_modules=str(temp / "extmods"),
        id="client-" + UUID,
        file_roots={"base": [str(ROOT / "salt/states")]},
    )
    opts["grains"] = {"id": opts["id"]}
    opts["pillar"] = {
        "schema": 1,
        "instance_uuid": UUID,
        "client": {"expires_at": None},
        "storage": {"device": "/dev/disk/by-id/test", "mount": "/srv/oduflow/data"},
        "backup": {
            "enabled": True,
            "provider": "cloudflare_r2",
            "bucket": "oduflow-" + UUID,
            "jurisdiction": "eu",
            "endpoint": "https://" + "a" * 32 + ".eu.r2.cloudflarestorage.com",
            "access_key_id": "b" * 32,
            "secret_access_key": "c" * 64,
            "repository_password": "fixture-repository-password-long",
            "retention_days": 30,
        },
        "versions": {"salt": "3006.27"},
    }
    functions = salt.loader.minion_mods(opts)
    renderers = salt.loader.render(opts, functions)
    state = salt.state.State(opts, initial_pillar=opts["pillar"])
    for name in ("oduflow/storage.sls", "roles/client_image.sls"):
        high = salt.template.compile_template(
            str(ROOT / "salt/states" / name), renderers, "jinja|yaml", [], []
        )
        errors = []
        salt.state.HighState._handle_state_decls(None, high, name, "base", errors)
        errors.extend(state.verify_high(high))
        chunks = state.compile_high_data(high)
        if errors:
            raise SystemExit("\n".join(errors))
        assert chunks, name
        for chunk in chunks:
            if chunk["state"] == "cmd" and chunk["__id__"].startswith("oduflow-storage-"):
                assert chunk["name"].startswith(
                    ("/usr/local/libexec/oduflow-storage", "systemctl ")
                ), chunk
        print(f"{name}: {len(chunks)} low-state chunks compiled; no states executed")
    with salt.state.HighState(opts, pillar_override=opts["pillar"]) as highstate:
        backup_high, errors = highstate.render_highstate({"base": ["client_backup"]})
    if errors:
        raise SystemExit("\n".join(errors))
    assert "client-backup-invalid-pillar" not in backup_high
    assert "client-backup-initial-restore-test" in backup_high
    print(f"client_backup/init.sls: {len(backup_high)} high-state declarations compiled")
    print("Master fingerprint calculation matches Salt pem_finger")
