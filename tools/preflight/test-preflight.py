import importlib.util
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight = load('preflight', HERE / 'preflight.py')
plan = load('plan', HERE.parent.parent / '.github/mach-lib/plan.py')

WORKFLOW = """
on:
  pull_request:
  workflow_dispatch:
    inputs:
      heavy: {type: choice, options: [none, all]}
jobs:
  lib:
    uses: briar-systems/.github/.github/workflows/mach-lib.yml@main
    with:
      heavy: ${{ inputs.heavy }}
      submodules: recursive
      fmt: false
      skip-legs: '["x86_64-windows"]'
      subprojects: '[{"path": "test/acme", "clean-dep": true}]'
  gate:
    needs: [lib]
    runs-on: ubuntu-latest
    steps: []
"""


class LibInputs(unittest.TestCase):
    def test_reads_the_lib_job_as_a_pull_request_sees_it(self):
        inputs, extra = preflight.lib_inputs(WORKFLOW, plan)
        self.assertEqual(inputs['heavy'], 'none')
        self.assertFalse(inputs['fmt'])
        self.assertTrue(inputs['test'])
        self.assertEqual(json.loads(inputs['legs']), plan.DEFAULT_LEGS)
        self.assertEqual(inputs['skip-legs'], '["x86_64-windows"]')
        self.assertEqual(extra, {'submodules': 'recursive', 'mach-version': ''})

    def test_the_inputs_plan(self):
        inputs, _ = preflight.lib_inputs(WORKFLOW, plan)
        matrix, config = plan.plan(inputs, 'main', '/nonexistent')
        self.assertNotIn('x86_64-windows', [e['leg']['name'] for e in matrix['include']])
        self.assertEqual(config['subprojects'][0]['path'], 'test/acme')

    def test_a_workflow_without_one_lib_job_is_refused(self):
        with self.assertRaises(ValueError):
            preflight.lib_inputs('jobs:\n  gate:\n    runs-on: ubuntu-latest\n', plan)


class DepSymlinks(unittest.TestCase):
    def test_committed_symlinks_at_or_under_dep(self):
        listing = '\n'.join([
            '120000 aaaa 0\tdep',
            '120000 bbbb 0\ttest/x/dep/std',
            '120000 cccc 0\tdocs/deploy',
            '100644 dddd 0\tdep/README',
        ])
        self.assertEqual(preflight.committed_dep_symlinks(listing), ['dep', 'test/x/dep/std'])

    def test_realized_dep_symlinks(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            (base / 'real').mkdir()
            (base / 'a' / 'dep').mkdir(parents=True)
            (base / 'b').mkdir()
            os.symlink(base / 'real', base / 'b' / 'dep')
            self.assertEqual(preflight.dep_symlinks(base), [str(Path('b/dep'))])


class Config(unittest.TestCase):
    def test_every_default_leg_runner_has_a_host(self):
        config = tomllib.loads((HERE / 'preflight.toml').read_text())
        for leg in plan.DEFAULT_LEGS:
            self.assertIn(leg['runs-on'], config['hosts'])
        for host in config['hosts'].values():
            self.assertEqual(set(host), {'isa', 'os'})

    def test_samples_are_not_excluded(self):
        config = tomllib.loads((HERE / 'preflight.toml').read_text())
        self.assertFalse(set(config['sample']['repos']) & set(config['exclude']))


if __name__ == '__main__':
    unittest.main()
