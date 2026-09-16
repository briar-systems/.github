import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('plan', Path(__file__).with_name('plan.py'))
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


def inputs(**overrides):
    base = {
        'legs': json.dumps(plan.DEFAULT_LEGS),
        'extra-legs': '[]',
        'skip-legs': '[]',
        'light-legs': '[]',
        'heavy': '',
        'profiles': '["debug", "release"]',
        'subprojects': '[]',
        'project': '.',
        'hooks-dir': '.github/ci',
        'test': True,
        'fmt': True,
        'all-targets': True,
        'timeout-minutes': 40,
    }
    base.update(overrides)
    return base


def run(base_ref='dev', root='/nonexistent', **overrides):
    matrix, config = plan.plan(inputs(**overrides), base_ref, root)
    return [entry['leg'] for entry in matrix['include']], config


class Tiers(unittest.TestCase):
    def test_dev_runs_only_light_legs(self):
        legs, config = run('dev')
        self.assertEqual([leg['name'] for leg in legs], ['x86_64-linux'])
        self.assertEqual((config['tier'], config['heavy']), ('light', ''))
        self.assertEqual(legs[0]['run-tier'], 'light')

    def test_main_runs_every_leg_heavy(self):
        legs, config = run('main')
        self.assertEqual(len(legs), len(plan.DEFAULT_LEGS))
        self.assertEqual((config['tier'], config['heavy']), ('heavy', 'all'))
        self.assertTrue(all(leg['run-tier'] == 'heavy' for leg in legs))

    def test_dispatch_all_matches_main(self):
        self.assertEqual(run('', heavy='all'), run('main'))

    def test_dispatch_none_is_light(self):
        self.assertEqual(run('', heavy='none'), run('dev'))

    def test_dispatch_one_leg_pulls_only_that_leg(self):
        legs, config = run('', heavy='x86_64-darwin')
        self.assertEqual([(leg['name'], leg['run-tier']) for leg in legs],
                         [('x86_64-linux', 'light'), ('x86_64-darwin', 'heavy')])
        self.assertEqual((config['tier'], config['heavy']), ('light', 'x86_64-darwin'))

    def test_dispatch_names_the_caller_owns_pass_through(self):
        legs, config = run('', heavy='assurance')
        self.assertEqual([leg['name'] for leg in legs], ['x86_64-linux'])
        self.assertEqual(config['heavy'], 'assurance')

    def test_primary_is_first_light_leg(self):
        legs, _ = run('main')
        self.assertEqual([leg['name'] for leg in legs if leg['primary']], ['x86_64-linux'])

    def test_fmt_runs_on_the_light_tier(self):
        legs, config = run('dev')
        self.assertTrue(config['fmt'])
        self.assertEqual([leg['name'] for leg in legs if leg['primary']], ['x86_64-linux'])

    def test_fmt_without_a_light_leg_is_refused(self):
        with self.assertRaises(plan.PlanError):
            run('main', **{'skip-legs': '["x86_64-linux"]'})

    def test_primary_falls_to_first_leg_when_no_light_leg_runs(self):
        legs, _ = run('main', fmt=False, **{'skip-legs': '["x86_64-linux"]'})
        self.assertEqual([leg['name'] for leg in legs if leg['primary']], ['aarch64-linux'])


class Legs(unittest.TestCase):
    def test_extra_legs_append(self):
        extra = '[{"name": "riscv64-linux", "runs-on": "ubuntu-latest", "tier": "heavy", "target": "linux-riscv64", "runner": "qemu-riscv64", "apt": ["qemu-user"]}]'
        legs, _ = run('main', **{'extra-legs': extra})
        self.assertEqual(legs[-1]['runner'], 'qemu-riscv64')
        self.assertEqual(legs[-1]['apt'], ['qemu-user'])

    def test_extra_args_are_kept_per_command(self):
        legs, _ = run('main', legs='[{"name": "darwin", "runs-on": "macos-15-intel", "build-args": ["--pie"]}]')
        self.assertEqual((legs[0]['build-args'], legs[0]['test-args']), (['--pie'], []))

    def test_legs_replace_the_defaults(self):
        legs, _ = run('main', legs='[{"name": "spirv", "runs-on": "ubuntu-latest", "target": "spirv", "test": false}]')
        self.assertEqual([(leg['name'], leg['test'], leg['timeout']) for leg in legs], [('spirv', False, 40)])

    def test_light_legs_promote_default_legs(self):
        legs, _ = run('dev', **{'light-legs': '["x86_64-windows", "aarch64-darwin"]'})
        self.assertEqual([(leg['name'], leg['run-tier'], leg['primary']) for leg in legs],
                         [('x86_64-linux', 'light', True), ('x86_64-windows', 'light', False),
                          ('aarch64-darwin', 'light', False)])

    def test_promoted_legs_run_heavy_into_main(self):
        legs, _ = run('main', **{'light-legs': '["x86_64-windows"]'})
        self.assertTrue(all(leg['run-tier'] == 'heavy' for leg in legs))

    def test_any_named_profile_passes_the_plan(self):
        _, config = run('dev', profiles='["debug", "release", "reassoc"]')
        self.assertEqual(config['profiles'], ['debug', 'release', 'reassoc'])

    def test_skip_removes_a_leg(self):
        legs, _ = run('main', **{'skip-legs': '["x86_64-windows"]'})
        self.assertNotIn('x86_64-windows', [leg['name'] for leg in legs])

    def test_every_leg_skipped_is_an_empty_matrix(self):
        legs, _ = run('dev', fmt=False, **{'skip-legs': '["x86_64-linux"]'})
        self.assertEqual(legs, [])

    def test_refusals(self):
        cases = {
            'unknown key': {'legs': '[{"name": "a", "runs-on": "x", "arch": "y"}]'},
            'missing runs-on': {'legs': '[{"name": "a"}]'},
            'bad tier': {'legs': '[{"name": "a", "runs-on": "x", "tier": "medium"}]'},
            'runner without target': {'legs': '[{"name": "a", "runs-on": "x", "runner": "qemu"}]'},
            'bool timeout': {'legs': '[{"name": "a", "runs-on": "x", "timeout": true}]'},
            'string test': {'legs': '[{"name": "a", "runs-on": "x", "test": "false"}]'},
            'bad name': {'legs': '[{"name": "A B", "runs-on": "x"}]'},
            'duplicate': {'extra-legs': '[{"name": "x86_64-linux", "runs-on": "x"}]'},
            'unknown skip': {'skip-legs': '["x86_64-linx"]'},
            'unknown light': {'light-legs': '["x86_64-windws"]'},
            'non-string light': {'light-legs': '[1]'},
            'empty profiles': {'profiles': '[]'},
            'bad profile name': {'profiles': '["debug", "Fast Math"]'},
            'not json': {'legs': '[{name: a}]'},
            'not array': {'legs': '{}'},
            'string build-args': {'legs': '[{"name": "a", "runs-on": "x", "build-args": "--pie"}]'},
            'non-string env': {'legs': '[{"name": "a", "runs-on": "x", "env": {"K": 1}}]'},
        }
        for label, overrides in cases.items():
            with self.subTest(label), self.assertRaises(plan.PlanError):
                run('main', **overrides)


class Subprojects(unittest.TestCase):
    def test_defaults(self):
        _, config = run('dev', subprojects='[{"path": "test/acme"}]')
        self.assertEqual(config['subprojects'], [{
            'path': 'test/acme', 'pull': True, 'build': False, 'test': True,
            'fmt': True, 'clean-dep': False, 'jobs': 0, 'legs': [], 'tier': 'light'}])

    def test_fmt_opt_out(self):
        _, config = run('dev', subprojects='[{"path": "a", "fmt": false}]')
        self.assertFalse(config['subprojects'][0]['fmt'])

    def test_refusals(self):
        cases = {
            'unknown leg': '[{"path": "a", "legs": ["x86_64-linx"]}]',
            'unknown key': '[{"path": "a", "clean": true}]',
            'test without pull': '[{"path": "a", "pull": false}]',
            'bad tier': '[{"path": "a", "tier": "medium"}]',
            'string fmt': '[{"path": "a", "fmt": "no"}]',
            'missing path': '[{"test": true}]',
        }
        for label, subprojects in cases.items():
            with self.subTest(label), self.assertRaises(plan.PlanError):
                run('dev', subprojects=subprojects)

    def test_pull_only(self):
        _, config = run('dev', subprojects='[{"path": "a", "test": false}]')
        self.assertFalse(config['subprojects'][0]['test'])


class Hooks(unittest.TestCase):
    def test_missing_directory_has_no_hooks(self):
        _, config = run('dev')
        self.assertEqual(config['hooks'], [])

    def test_known_hooks_are_listed(self):
        with tempfile.TemporaryDirectory() as root:
            hooks = Path(root) / '.github/ci'
            hooks.mkdir(parents=True)
            (hooks / 'verify.sh').write_text('')
            (hooks / 'setup.sh').write_text('')
            _, config = run('dev', root=root)
            self.assertEqual(config['hooks'], ['setup.sh', 'verify.sh'])

    def test_a_misspelled_hook_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            hooks = Path(root) / '.github/ci'
            hooks.mkdir(parents=True)
            (hooks / 'verfy.sh').write_text('')
            with self.assertRaises(plan.PlanError):
                run('dev', root=root)

    def test_teardown_without_setup_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            hooks = Path(root) / '.github/ci'
            hooks.mkdir(parents=True)
            (hooks / 'teardown.sh').write_text('')
            with self.assertRaises(plan.PlanError):
                run('dev', root=root)


if __name__ == '__main__':
    unittest.main()
