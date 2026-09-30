"""Dependency/build contracts; safe to run with stdlib before dependencies install."""
from pathlib import Path
import hashlib
import os
from types import SimpleNamespace
from unittest.mock import patch
import shutil
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DevDependencyContracts(unittest.TestCase):
    def test_extras_separate_browser_from_backend(self):
        project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
        extras = project['project']['optional-dependencies']
        self.assertFalse(any('playwright' in item for item in extras['dev']))
        self.assertTrue(any('playwright' in item for item in extras['browser']))
        self.assertIn('pytest-xdist==3.8.0', extras['dev'])

    def test_lock_covers_metadata_and_build_backend(self):
        project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
        lock = tomllib.loads((ROOT / 'uv.lock').read_text())
        packages = {p['name']: p for p in lock['package']}
        root = packages['open-trader']
        self.assertEqual(set(project['project']['optional-dependencies']),
                         set(root['optional-dependencies']))
        for name, version in [('pytest-xdist', '3.8.0'),
                              ('tencentcloud-sdk-python-ssm', '3.1.160'),
                              ('tencentcloud-sdk-python-common', '3.1.182')]:
            self.assertEqual(packages[name]['version'], version)
        backend = project['build-system']['requires'][0]
        self.assertEqual(backend, 'setuptools==' + packages['setuptools']['version'])

    def test_docker_consumes_locked_dependencies(self):
        docker = (ROOT / 'Dockerfile.dev').read_text()
        self.assertRegex(docker, r'FROM python:3\.12\.\d+-slim-bookworm@sha256:[a-f0-9]{64} AS dev')
        self.assertRegex(docker, r'FROM .*uv:.*@sha256:[a-f0-9]{64} AS uv')
        self.assertIn('COPY pyproject.toml uv.lock', docker)
        self.assertIn('uv sync --locked', docker)
        self.assertIn('--extra dev --extra cloud-ssm', docker)
        self.assertIn('--no-build-isolation', docker)
        self.assertNotIn('pip install', docker)
        self.assertNotIn('--extra browser', docker)
        self.assertIn('test-only', docker)

    @unittest.skipUnless(shutil.which("uv"), "uv is needed for the stale-lock check")
    def test_stale_lock_fails_without_rewriting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = (ROOT / "uv.lock").read_bytes()
            (root / "uv.lock").write_bytes(original)
            metadata = root / "pyproject.toml"
            metadata.write_text((ROOT / "pyproject.toml").read_text())
            env = dict(os.environ, UV_CACHE_DIR=str(root / "cache"))
            command = ["uv", "lock", "--check", "--offline", "--no-python-downloads"]
            control = subprocess.run(command, cwd=root, env=env, capture_output=True,
                                     text=True, timeout=30)
            self.assertEqual(control.returncode, 0, control.stderr)
            # Remove dependencies, so recomputation needs no registry metadata.
            # This must fail specifically for a stale lock, not a cache/network error.
            metadata.write_text('[project]\nname="open-trader"\nversion="0.1.0"\n'
                                'requires-python=">=3.12"\n')
            result = subprocess.run(command, cwd=root, env=env, capture_output=True,
                                    text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("needs to be updated", result.stderr)
            self.assertIn("--check", result.stderr)
            self.assertEqual((root / "uv.lock").read_bytes(), original)

    def test_dependency_manifest_records_identity_and_sorted_versions(self):
        from scripts.dev_dependency_manifest import manifest
        packages = [SimpleNamespace(metadata={"Name": "Z_example"}, version="2"),
                    SimpleNamespace(metadata={"Name": "alpha"}, version="1")]
        with patch("scripts.dev_dependency_manifest.distributions", return_value=packages), \
             patch.dict("os.environ", {"OPEN_TRADER_TEST_SOURCE_SHA": "a" * 40,
                                       "OPEN_TRADER_TEST_SOURCE_STATE": "dirty"}):
            result = manifest(ROOT)
        self.assertEqual(result["dependencies"], [("alpha", "1"), ("z-example", "2")])
        self.assertEqual(result["source_sha"], "a" * 40)
        self.assertEqual(result["source_state"], "dirty")
        self.assertEqual(result["lock_sha256"], hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest())
        self.assertIn("test-only", result["role"])
        self.assertEqual(len(result["base_images"]), 2)

    def test_runtime_isolation_contract(self):
        make = (ROOT / 'Makefile').read_text()
        run = next(line for line in make.splitlines() if line.startswith('DOCKER_RUN ='))
        for option in ('--network none', '--cap-drop ALL', '--security-opt no-new-privileges'):
            self.assertIn(option, run)
        for forbidden in ('--volume', '--mount', ' -v ', '--env-file', ' -e ', '--privileged', '--publish'):
            self.assertNotIn(forbidden, run)
        ignores = (ROOT / '.dockerignore').read_text().splitlines()
        for path in ('.git', '.env', '.ssh', '.aws', 'data/*', 'logs',
                     'config/prediction_arbitrage.json', '*.key', '*.pem'):
            self.assertIn(path, ignores)


if __name__ == '__main__':
    unittest.main()
