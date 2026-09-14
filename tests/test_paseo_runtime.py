"""Runtime archive compatibility and Salt source selection."""

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_client_apps import ARTIFACTS, args, high, installer


class RuntimeInstallation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.target = self.root / "installed"
        self.target.mkdir()
        self.node = installer.node_bin(ARTIFACTS)
        self.metadata = {
            "contract": 1,
            "os": "ubuntu",
            "os_version": "26.04",
            "architecture": "x86_64",
            "node_version": ARTIFACTS["node"]["version"],
            "version": ARTIFACTS["paseo"]["version"],
            "commit": ARTIFACTS["paseo"]["commit"],
        }
        for item in (
            patch.object(installer, "CACHE", self.cache),
            patch.object(
                installer.platform,
                "freedesktop_os_release",
                return_value={"ID": "ubuntu", "VERSION_ID": "26.04"},
            ),
            patch.object(installer.platform, "machine", return_value="x86_64"),
        ):
            item.start()
            self.addCleanup(item.stop)

    def bundle(self, metadata=None, extra=None):
        contents = {
            "runtime.json": json.dumps(metadata or self.metadata).encode(),
            "runtime/" + installer.SERVER_ENTRY: b"console.log('fixture');",
            "runtime/bin/paseo": b"#!/usr/bin/env node\n",
        }
        contents.update(extra or {})
        path = self.cache / "paseo-runtime.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data in contents.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755
                archive.addfile(info, io.BytesIO(data))
        return dict(
            ARTIFACTS["paseo"],
            runtime={"hash": "sha256=" + hashlib.sha256(path.read_bytes()).hexdigest()},
        )

    def test_installs_complete_runtime_without_npm(self):
        spec = self.bundle()
        with patch.object(installer, "run") as run:
            installer.install_paseo_runtime(spec, self.target, self.node, {})
        self.assertTrue((self.target / installer.SERVER_ENTRY).is_file())
        self.assertTrue((self.target / "bin/paseo").is_file())
        run.assert_called_once_with(
            [str(self.node / "node"), "--check", str(self.target / installer.SERVER_ENTRY)], {}
        )
        self.assertFalse((self.cache / "paseo-runtime-stage").exists())

    def test_incompatible_runtime_preserves_existing_install(self):
        existing = self.target / "keep"
        existing.write_text("original")
        for key, value in (
            ("os_version", "24.04"),
            ("architecture", "aarch64"),
            ("commit", "a" * 40),
            ("node_version", "20.0.0"),
        ):
            with self.subTest(key=key):
                spec = self.bundle(dict(self.metadata, **{key: value}))
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    installer.install_paseo_runtime(spec, self.target, self.node, {})
                self.assertEqual(existing.read_text(), "original")

    def test_checksum_and_path_traversal_refuse(self):
        spec = self.bundle()
        spec["runtime"]["hash"] = "sha256=" + "0" * 64
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            installer.install_paseo_runtime(spec, self.target, self.node, {})
        spec = self.bundle(extra={"runtime/../../outside": b"bad"})
        with self.assertRaises(tarfile.FilterError):
            installer.install_paseo_runtime(spec, self.target, self.node, {})
        self.assertFalse((self.cache / "outside").exists())

    def test_salt_prebuilt_requires_archive_and_omits_source(self):
        runtime = {"source": "salt://artifacts/paseo.tar.gz", "hash": "sha256=" + "a" * 64}
        data = high(
            "client_apps/install.sls", {"paseo": {"install_method": "prebuilt", "runtime": runtime}}
        )
        self.assertNotIn("client-apps-paseo-artifact", data)
        self.assertEqual(args(data["client-apps-paseo-runtime"])["source_hash"], runtime["hash"])
        manifest = json.loads(args(data["client-apps-manifest"])["contents"])
        self.assertEqual(manifest["paseo"]["install_method"], "prebuilt")
        self.assertIn(
            {"file": "client-apps-paseo-runtime"},
            args(data["client-apps-install-paseo"])["require"],
        )
        for paseo in (
            {"install_method": "wrong"},
            {"install_method": "prebuilt"},
            {"install_method": "prebuilt", "runtime": dict(runtime, hash="md5=bad")},
        ):
            data = high("client_apps/install.sls", {"paseo": paseo})
            self.assertEqual(list(data), ["client-apps-invalid-install-method"])
