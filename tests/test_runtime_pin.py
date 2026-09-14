"""Offline distribution-contract checks for the bundled Puppetmaster runtime."""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def runtime_pin():
    return re.search(r'puppetmaster-ai==(\d+\.\d+\.\d+)',
                     (ROOT / 'pyproject.toml').read_text()).group(1)


class RuntimePinTests(unittest.TestCase):
    def test_operational_surfaces_match_pyproject(self):
        for name in (
            'scripts/install.sh', 'scripts/install.ps1',
            'scripts/doctor.sh', 'scripts/doctor.ps1',
            'harness/diag_bundle.py',
            'webapp/electron/bootstrap.cjs', 'webapp/electron/update-pm.cjs',
            '.github/workflows/tests.yml', '.github/workflows/tests-full.yml',
            '.github/workflows/release.yml',
            'README.md', 'CONTRIBUTING.md', 'FINDINGS.md',
        ):
            with self.subTest(path=name):
                pins = re.findall(r'puppetmaster-ai==(\d+\.\d+\.\d+)',
                                  (ROOT / name).read_text())
                self.assertTrue(pins, 'runtime pin missing')
                self.assertEqual(set(pins), {runtime_pin()})
        lock = (ROOT / 'uv.lock').read_text()
        self.assertIn('name = "puppetmaster-ai"\nversion = "' + runtime_pin() + '"', lock)
        self.assertIn('specifier = "==' + runtime_pin() + '"', lock)

    def test_marionette_release_versions_match(self):
        from harness import __version__
        self.assertIn('version = "' + __version__ + '"',
                      (ROOT / 'pyproject.toml').read_text())
        self.assertIn('Current release: **v' + __version__ + '**',
                      (ROOT / 'README.md').read_text())
        package = json.loads((ROOT / 'webapp/package.json').read_text())
        lock = json.loads((ROOT / 'webapp/package-lock.json').read_text())
        self.assertEqual(package['version'], __version__)
        self.assertEqual(lock['version'], __version__)
        self.assertEqual(lock['packages']['']['version'], __version__)
        self.assertIn('name = "pm-harness"\nversion = "' + __version__ + '"',
                      (ROOT / 'uv.lock').read_text())

    def test_installed_codex_catalog_acceptance_isolated(self):
        # -I and an empty cwd exclude checkout/PYTHONPATH shadowing. CI installs
        # the pinned distribution; no credentials, CLI, or provider calls needed.
        script = r'''
import importlib.metadata
import sys

def offline(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo'):
        raise AssertionError('network forbidden in catalog acceptance')
sys.addaudithook(offline)
assert importlib.metadata.version('puppetmaster-ai') == sys.argv[1]
from puppetmaster import openai_codex as codex
from puppetmaster.static_catalog import curated_catalog
native = curated_catalog('codex')
oauth = [row for row in curated_catalog('agentic')
         if (row.get('payload_defaults') or {}).get('provider') == 'openai-codex']
assert native and oauth
for row in native + oauth:
    model = row['model']
    assert codex.harden_codex_model_id(model) == (model, None), model
for model in ('gpt-6-astra', 'gpt-5.3-codex'):
    for prefix in ('', 'openai-codex/', 'agentic/openai-codex/', 'openai-codex:'):
        assert codex.harden_codex_model_id(prefix + model) == (model, None)
try:
    codex.harden_codex_model_id('gpt-6-imaginary')
except codex.UnknownCodexModelError:
    pass
else:
    raise AssertionError('unknown model must fail closed')
'''
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run(
                [sys.executable, '-I', '-c', script, runtime_pin()],
                cwd=cwd, capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
