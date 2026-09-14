"""Client app state contracts; no host packages, services, or cloud writes."""

import copy
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
STATES = ROOT / "salt/states"
UUID = "12345678-1234-1234-1234-123456789abc"
PILLAR = {
    "schema": 1,
    "instance_uuid": UUID,
    "storage": {"device": "/dev/disk/by-id/test", "mount": "/srv/oduflow/data"},
    "dns": {
        "production": "acme.example.com",
        "oduflow": "oduflow.acme.example.com",
        "paseo": "paseo.acme.example.com",
    },
    "ingress": {"mode": "direct_tls", "acme": {"email": "admin@example.com"}},
    "oduflow": {
        "auth_token": "fixture-auth-token-not-real",
        "ui_password": "fixture-ui-password-not-real",
        "database_password": "fixture-database-password-not-real",
    },
    "paseo": {"password": "fixture-paseo-password-not-real"},
}


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, STATES / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ARTIFACTS = json.loads((STATES / "client_apps/artifacts.json").read_text())
PASEO_RELEASE = ARTIFACTS["paseo"]["version"] + "+" + ARTIFACTS["paseo"]["commit"][:12]

installer = load_module("app_installer", "client_apps/files/install.py")
preflight = load_module("docker_preflight", "client_apps/files/docker-preflight.py")


class Loader(jinja2.FileSystemLoader):
    def get_source(self, env, template):
        source, path, current = super().get_source(env, template)
        # Salt's import_json is replaced with the same parsed object in this
        # dependency-light unit harness. Actual Salt compilation is separate.
        source = source.replace("{% import_json 'client_apps/artifacts.json' as artifacts %}", "")
        return source, path, current


def render(path, pillar=None):
    pillar = copy.deepcopy(PILLAR if pillar is None else pillar)

    def get(key, default=None):
        value = pillar
        for part in key.split(":"):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    env = jinja2.Environment(loader=Loader(str(STATES)), undefined=jinja2.StrictUndefined)
    env.filters["regex_match"] = lambda value, pattern: re.match(pattern, value)
    return env.get_template(path).render(
        salt={"pillar.get": get},
        grains={"id": "client-" + UUID, "os": "Ubuntu", "cpuarch": "x86_64"},
        artifacts=copy.deepcopy(ARTIFACTS),
    )


def high(path, pillar=None):
    return yaml.safe_load(render(path, pillar))


def args(state):
    return {key: value for item in next(iter(state.values())) for key, value in item.items()}


class StateContracts(unittest.TestCase):
    def test_install_needs_no_credentials_and_starts_no_services(self):
        data = high("client_apps/install.sls", {})
        self.assertIn("client-apps-install-oduflow", data)
        self.assertIn("client-apps-install-paseo", data)
        self.assertFalse(any("service.running" in state for state in data.values()))
        for key in ["uv", "node"]:
            spec = args(data["client-apps-" + key])
            self.assertRegex(spec["source_hash"], r"^sha256=[a-f0-9]{64}$")
        self.assertIn("source_hash", args(data["client-apps-oduflow-artifact"]))
        # The private source archive is pinned by both its commit and checksum.
        paseo = ARTIFACTS["paseo"]
        self.assertRegex(paseo["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(paseo["repository"], "https://github.com/oduflow/paseo.git")
        self.assertEqual(
            args(data["client-apps-paseo-artifact"])["source_hash"], paseo["archive"]["hash"]
        )
        manifest = args(data["client-apps-manifest"])
        self.assertEqual(json.loads(manifest["contents"])["paseo"]["commit"], paseo["commit"])

    def test_bad_identity_or_secret_fails_without_runtime_states(self):
        for target in ["oduflow/init.sls", "paseo/init.sls", "client_apps/start.sls"]:
            for field, value in [
                ("schema", 2),
                ("instance_uuid", "wrong"),
                ("paseo", {}),
                ("paseo", {"password": "bad\nEnvironment=unsafe"}),
            ]:
                pillar = copy.deepcopy(PILLAR)
                pillar[field] = value
                data = high(target, pillar)
                self.assertEqual(len(data), 1)
                self.assertEqual(list(next(iter(data.values()))), ["test.fail_without_changes"])

    def test_paseo_config_is_valid_json_and_password_stays_private(self):
        data = high("paseo/init.sls")
        config = json.loads(args(data["paseo-config"])["contents"])
        self.assertEqual(config["daemon"]["listen"], "127.0.0.1:6767")
        self.assertFalse(config["daemon"]["relay"]["enabled"])
        self.assertNotIn("providers", config)
        self.assertEqual(
            config["agents"]["providers"],
            {
                name: {"enabled": name == "opencode"}
                for name in ["claude", "codex", "copilot", "opencode", "pi", "omp"]
            },
        )
        password = args(data["paseo-password"])
        self.assertEqual(password["mode"], "0600")
        self.assertFalse(password["show_changes"])
        self.assertEqual(
            password["contents"],
            "PASEO_PASSWORD=fixture-paseo-password-not-real\nODUFLOW_MCP_TOKEN=fixture-auth-token-not-real\n",
        )

    def test_config_and_mount_guards_precede_docker_package(self):
        data = high("client_apps/docker.sls")
        requires = args(data["client-apps-docker-package"])["require"]
        self.assertIn({"file": "client-apps-docker-data-root"}, requires)
        self.assertIn({"file": "client-apps-containerd-data-root"}, requires)
        self.assertIn({"cmd": "client-apps-docker-reload"}, requires)
        for service in ["docker", "containerd"]:
            content = args(data["client-apps-" + service + "-guard"])["contents"]
            self.assertIn("BindsTo=srv-oduflow-data.mount", content)
            self.assertIn("--verify --timeout 0", content)
        docker = json.loads(args(data["client-apps-docker-data-root"])["contents"])
        self.assertEqual(docker["data-root"], "/srv/oduflow/data/docker")
        self.assertFalse(docker["live-restore"])
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        containerd = tomllib.loads(args(data["client-apps-containerd-data-root"])["contents"])
        self.assertEqual(containerd["root"], "/srv/oduflow/data/containerd")
        # Containerd 2 rejects short plugin IDs; retain compatibility with 1.x CRI too.
        self.assertEqual(
            set(containerd["disabled_plugins"]),
            {
                "io.containerd.grpc.v1.cri",
                "io.containerd.cri.v1.images",
                "io.containerd.cri.v1.runtime",
            },
        )

    def test_public_auth_and_production_opt_in_without_production_creation(self):
        config = render("oduflow/files/oduflow.toml.jinja")
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        config = tomllib.loads(config)
        self.assertEqual(config["routing"]["mode"], "traefik")
        self.assertTrue(config["routing"]["tls"])
        self.assertTrue(config["production"]["enabled"])
        self.assertFalse(config["server"]["allow_insecure_http"])
        self.assertEqual(config["server"]["bind"], "172.17.0.1")
        self.assertNotIn("host", config["server"])
        self.assertNotIn("oauth", config)
        self.assertNotIn("hostname", config["routing"])
        self.assertEqual(config["team"]["1"]["hostname"], PILLAR["dns"]["oduflow"])
        self.assertEqual(config["database"]["password"], PILLAR["oduflow"]["database_password"])
        self.assertNotEqual(config["database"]["password"], config["team"]["1"]["auth_token"])
        self.assertEqual(config["team"]["1"]["environment_hostname_mode"], "branch")
        self.assertEqual(config["route"]["paseo"]["url"], "http://172.17.0.1:6768")

    def test_missing_database_credential_cannot_fall_back_to_mcp_token(self):
        pillar = copy.deepcopy(PILLAR)
        del pillar["oduflow"]["database_password"]
        data = high("client_apps/start.sls", pillar)
        self.assertEqual(list(data), ["client-apps-start-invalid-pillar"])
        self.assertNotIn("include", data)

    def test_units_preserve_published_cli_and_private_proxy(self):
        startup = args(high("client_apps/start.sls")["client-apps-paseo-running"])
        self.assertIn({"cmd": "client-apps-install-paseo"}, startup["watch"])
        oduflow_unit = (STATES / "oduflow/files/oduflow.service").read_text()
        self.assertIn("UMask=0022", oduflow_unit)
        managed = high("oduflow/init.sls")
        self.assertEqual(args(managed["oduflow-config"])["mode"], "0600")
        for name in ["postgresql.conf", "postgresql-prod.conf"]:
            repair = args(managed["oduflow-generated-permissions-" + name])
            self.assertEqual(repair["mode"], "0644")
            self.assertFalse(repair["create"])
            self.assertFalse(repair["replace"])
        unit = render("paseo/files/paseo.service.jinja")
        # The source build rejects the removed launch flags; persistent config and
        # `daemon run` are the supported deployment surface.
        self.assertIn(
            "ExecStart=/opt/oduflow/paseo/" + PASEO_RELEASE + "/bin/paseo daemon run"
            " --home /srv/paseo\n",
            unit,
        )
        for flag in ["--foreground", "--no-relay", "--web-ui", "--listen"]:
            self.assertNotIn(flag, unit)
        self.assertIn("/opt/oduflow/node/" + ARTIFACTS["node"]["version"] + "/", unit)
        self.assertIn("User=paseo", unit)
        self.assertIn("EnvironmentFile=/etc/paseo/credentials.env", unit)
        self.assertNotIn("PASEO_PASSWORD=", unit)
        path = next(
            line.split("=", 2)[2]
            for line in unit.splitlines()
            if line.startswith("Environment=PATH=")
        )
        self.assertIn("/usr/sbin", path.split(":"))  # root ExecStartPre needs wipefs/blkid
        self.assertIn("/sbin", path.split(":"))
        socket = (STATES / "paseo/files/paseo-proxy.socket").read_text()
        self.assertIn("ListenStream=172.17.0.1:6768", socket)
        self.assertNotIn("0.0.0.0", socket)

    def test_state_includes_and_requisites_resolve(self):
        all_data = {}

        def collect(name):
            path = name.replace(".", "/")
            path += ".sls" if (STATES / (path + ".sls")).exists() else "/init.sls"
            data = high(path) or {}
            for include in data.pop("include", []):
                collect(include)
            all_data.update(data)

        collect("roles.client_stack")
        extensions = all_data.pop("extend", {})
        for state in [*all_data.values(), *extensions.values()]:
            for item in next(iter(state.values())):
                for kind in ("require", "watch", "onchanges"):
                    for requisite in item.get(kind, []):
                        name = next(iter(requisite.values()))
                        self.assertIn(name, all_data, (kind, name))


class InstallReceipts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name, value in [("ROOT", self.tmp / "opt"), ("CACHE", self.tmp / "cache")]:
            patcher = patch.object(installer, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        installer.CACHE.mkdir(parents=True)
        legacy = copy.deepcopy(ARTIFACTS)
        legacy["paseo"].pop("archive", None)
        manifest = self.tmp / "manifest.json"
        manifest.write_text(json.dumps(legacy))
        patcher = patch.object(installer, "MANIFEST", manifest)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.receipt = installer.ROOT / "paseo" / PASEO_RELEASE / ".installed"
        self.executable = installer.ROOT / "paseo" / PASEO_RELEASE / "bin/paseo"

    def source_build(self, commit=None, version=None, packs=None):
        """Stand in for git and npm without reaching the network or a registry."""
        spec = ARTIFACTS["paseo"]
        source = installer.CACHE / "paseo-src"
        target = self.executable.parent.parent

        def fake(command, **kwargs):
            if command[-1] == "FETCH_HEAD":
                (source / "package.json").write_text(
                    json.dumps({"version": version or spec["version"], "scripts": {"prepare": "x"}})
                )
            if command[-1] == "HEAD":
                head = (commit or spec["commit"]) + "\n"
                return subprocess.CompletedProcess(command, 0, head, "")
            if "pack" in command:
                produced = len(list((installer.CACHE / "paseo-packs").glob("*.tgz")))
                if produced < (len(spec["packages"]) if packs is None else packs):
                    (installer.CACHE / "paseo-packs" / (str(produced) + ".tgz")).touch()
            if "install" in command and "--global" in command:
                self.executable.parent.mkdir(parents=True, exist_ok=True)
                self.executable.touch()
                entry = target / installer.SERVER_ENTRY
                entry.parent.mkdir(parents=True, exist_ok=True)
                entry.touch()
            return subprocess.CompletedProcess(command, 0, "", "")

        return fake

    @unittest.skipUnless(hasattr(tarfile, "data_filter"), "Requires the Ubuntu 24.04 tarfile API")
    def test_private_archive_checks_hash_commit_and_extraction_paths(self):
        spec = copy.deepcopy(ARTIFACTS["paseo"])
        path = installer.CACHE / ("paseo-" + spec["commit"] + ".tar.gz")
        source = installer.CACHE / "unpacked"

        def archive(commit, name="package.json"):
            content = json.dumps({"version": spec["version"]}).encode()
            with tarfile.open(
                path, "w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": commit}
            ) as output:
                item = tarfile.TarInfo(name)
                item.size = len(content)
                output.addfile(item, io.BytesIO(content))
            spec["archive"]["hash"] = "sha256=" + hashlib.sha256(path.read_bytes()).hexdigest()

        archive(spec["commit"])
        with patch.object(installer.subprocess, "run") as run:
            installer.checkout(spec, source, {})
            run.assert_not_called()
        self.assertEqual(
            json.loads((source / "package.json").read_text())["version"], spec["version"]
        )
        for failure in ("hash", "commit", "path"):
            shutil.rmtree(source)
            archive(
                "0" * 40 if failure == "commit" else spec["commit"],
                "../../escape" if failure == "path" else "package.json",
            )
            if failure == "hash":
                spec["archive"]["hash"] = "sha256=" + "0" * 64
            with self.assertRaises((RuntimeError, tarfile.TarError)):
                installer.checkout(spec, source, {})
        self.assertFalse((self.tmp / "escape").exists())

    def test_failed_install_never_writes_success_receipt(self):
        with patch.object(
            installer.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["npm"])
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install("paseo")
        self.assertFalse(self.receipt.exists())

    def test_success_is_idempotent_but_missing_binary_reinstalls(self):
        with patch.object(installer.subprocess, "run", side_effect=self.source_build()) as run:
            self.assertTrue(installer.install("paseo"))
            calls = run.call_count
            self.assertFalse(installer.install("paseo"))
            self.executable.unlink()
            self.assertTrue(installer.install("paseo"))
            self.assertEqual(run.call_count, 2 * calls)
        self.assertEqual(self.receipt.read_text(), PASEO_RELEASE + "\n")

    def test_unexpected_source_is_refused_before_any_installation(self):
        commit = "0" * 40
        for stub in [
            self.source_build(commit=commit),
            self.source_build(version="9.9.9"),
            self.source_build(packs=1),
        ]:
            with patch.object(installer.subprocess, "run", side_effect=stub):
                with self.assertRaises(RuntimeError):
                    installer.install("paseo")
            self.assertFalse(self.receipt.exists())
            shutil.rmtree(installer.ROOT, ignore_errors=True)

    def test_source_build_heap_limit_is_inherited_only_by_build_commands(self):
        with patch.object(installer.subprocess, "run", side_effect=self.source_build()) as run:
            installer.install("paseo")
        npm_calls = [call for call in run.call_args_list if Path(call.args[0][0]).name == "npm"]
        self.assertEqual(len(npm_calls), len(ARTIFACTS["paseo"]["packages"]) + 2)
        self.assertTrue(
            all(
                call.kwargs["env"]["NODE_OPTIONS"] == "--max-old-space-size=4096"
                for call in npm_calls
            )
        )
        git_calls = [call for call in run.call_args_list if call.args[0][0] == "git"]
        self.assertTrue(git_calls)
        self.assertTrue(
            all(
                call.kwargs["env"].get("NODE_OPTIONS") == os.environ.get("NODE_OPTIONS")
                for call in git_calls
            )
        )

    def test_build_tree_is_removed_even_when_the_build_fails(self):
        with patch.object(
            installer.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["npm"])
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install("paseo")
        self.assertFalse((installer.CACHE / "paseo-src").exists())
        self.assertFalse((installer.CACHE / "paseo-packs").exists())

    def test_existing_unmanaged_container_data_is_not_migrated(self):
        root = self.tmp
        docker = root / "docker"
        docker.mkdir()
        (docker / "important-data").write_text("keep")
        original_path = Path
        with patch.object(
            preflight,
            "Path",
            side_effect=lambda name: (
                docker if name == "/var/lib/docker" else original_path(root / name.lstrip("/"))
            ),
        ):
            with self.assertRaises(ValueError):
                preflight.check()
        self.assertEqual((docker / "important-data").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
