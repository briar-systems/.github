import importlib.util
import os
import unittest.mock
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('run', Path(__file__).with_name('run.py'))
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)

INFO = 'mach 5.1.0\nhost: linux-arm64\nisa: aarch64\nos: linux\nabi: aapcs64\nobject: elf\n'
HOST = {'name': 'linux-arm64', 'isa': 'aarch64', 'os': 'linux'}

MANIFEST = {
    'target': {
        'linux-x86_64': {'isa': 'x86_64', 'os': 'linux', 'abi': 'sysv64', 'default': True},
        'windows': {'isa': 'x86_64', 'os': 'windows', 'abi': 'win64'},
    },
    'profile': {'debug': {}, 'release': {}},
}


def leg(name='aarch64-linux', target='', test=True):
    return {'name': name, 'target': target, 'test': test}


class Host(unittest.TestCase):
    def test_parses_mach_info(self):
        self.assertEqual(run.parse_host(INFO), HOST)


class Manifests(unittest.TestCase):
    def problems(self, manifest=MANIFEST, **kwargs):
        profiles = kwargs.pop('profiles', ['debug', 'release'])
        tested = kwargs.pop('tested', True)
        return run.manifest_problems('p/mach.toml', manifest, leg(**kwargs), profiles, HOST, tested)

    def test_a_host_without_a_target_is_refused_despite_a_default(self):
        [problem] = self.problems()
        self.assertIn('declares no target for the host linux-arm64 of leg aarch64-linux', problem)

    def test_a_build_only_project_may_target_another_platform(self):
        spirv = dict(MANIFEST, target={'spirv': {'isa': 'spirv', 'os': 'freestanding'}})
        self.assertEqual(self.problems(spirv, tested=False), [])
        self.assertEqual(len(self.problems(spirv, tested=True)), 1)

    def test_a_declared_host_passes(self):
        manifest = dict(MANIFEST, target=dict(MANIFEST['target'], arm={'isa': 'aarch64', 'os': 'linux'}))
        self.assertEqual(self.problems(manifest), [])

    def test_a_named_target_must_be_declared(self):
        self.assertEqual(self.problems(target='windows'), [])
        self.assertEqual(self.problems(target='darwin'),
                         ['p/mach.toml declares no target darwin for leg aarch64-linux'])

    def test_a_manifest_without_targets_builds_for_the_host(self):
        self.assertEqual(self.problems({'profile': MANIFEST['profile']}), [])

    def test_profiles_must_be_declared(self):
        manifest = dict(MANIFEST, target={'arm': {'isa': 'aarch64', 'os': 'linux'}})
        self.assertEqual(self.problems(manifest, profiles=['debug', 'reassoc']),
                         ['p/mach.toml declares no profile reassoc'])


class LegManifests(unittest.TestCase):
    config = {'project': '.', 'test': True, 'subprojects': [
        {'path': 'a', 'build': False, 'test': True, 'legs': [], 'tier': 'light'},
        {'path': 'b', 'build': False, 'test': False, 'legs': [], 'tier': 'light'},
        {'path': 'c', 'build': True, 'test': False, 'legs': ['other'], 'tier': 'light'},
        {'path': 'd', 'build': True, 'test': False, 'legs': [], 'tier': 'heavy'},
    ]}

    def test_only_subprojects_the_leg_builds_or_tests(self):
        light = dict(leg(), **{'run-tier': 'light'})
        heavy = dict(leg(), **{'run-tier': 'heavy'})
        self.assertEqual(run.leg_manifests(light, self.config), [('.', True), ('a', True)])
        self.assertEqual(run.leg_manifests(heavy, self.config), [('.', True), ('a', True), ('d', False)])

    def test_a_build_only_leg_tests_nothing(self):
        build_only = dict(leg(test=False), **{'run-tier': 'light'})
        self.assertEqual(run.leg_manifests(build_only, self.config), [('.', False), ('a', False)])

    def test_a_repo_without_project_tests(self):
        config = dict(self.config, test=False)
        self.assertEqual(run.leg_manifests(dict(leg(), **{'run-tier': 'light'}), config)[0], ('.', False))


def sub(path, **keys):
    return dict({'path': path, 'build': True, 'test': False}, **keys)


class Deps(unittest.TestCase):
    def commands(self, config):
        calls = []
        real = run.mach
        run.mach = lambda *args, cwd=None: calls.append(' '.join(args))
        try:
            run.deps(dict(leg(), **{'run-tier': 'light'}), config)
        finally:
            run.mach = real
        return calls

    def test_each_mode_runs_its_command(self):
        config = {'project': '.', 'deps': 'pull', 'subprojects': [
            {'path': 'pinned', 'deps': 'pull', 'clean-dep': False, 'legs': [], 'tier': 'light'},
            {'path': 'examples/a', 'deps': 'update', 'clean-dep': False, 'legs': [], 'tier': 'light'},
            {'path': 'docs', 'deps': 'none', 'clean-dep': False, 'legs': [], 'tier': 'light'},
        ]}
        self.assertEqual(self.commands(config), [
            'dep pull .', 'dep pull pinned', 'dep update examples/a --all'])

    def test_the_root_can_update(self):
        config = {'project': 'app', 'deps': 'update', 'subprojects': []}
        self.assertEqual(self.commands(config), ['dep update app --all'])


class Expand(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        base = Path(self.root.name)
        for name in ('zeta', 'alpha', 'mid'):
            (base / 'examples' / name).mkdir(parents=True)
            (base / 'examples' / name / 'mach.toml').write_text('')
        (base / 'examples' / 'notes').mkdir()
        (base / 'examples' / 'README.md').write_text('')

    def tearDown(self):
        self.root.cleanup()

    def expand(self, *subs):
        return run.expand_subprojects(list(subs), self.root.name)

    def test_a_glob_expands_to_sorted_projects_with_the_entry_keys(self):
        expanded = self.expand(sub('examples/*', jobs=2))
        self.assertEqual([x['path'] for x in expanded], ['examples/alpha', 'examples/mid', 'examples/zeta'])
        self.assertTrue(all(x['jobs'] == 2 and x['build'] and not x['test'] for x in expanded))

    def test_matches_use_posix_separators(self):
        # a windows path would reach mach and the duplicate check with backslashes
        expanded = self.expand(sub('examples/*'))
        self.assertTrue(all('\\' not in x['path'] and x['path'].count('/') == 1 for x in expanded))

    def test_literal_paths_pass_through_in_order(self):
        expanded = self.expand(sub('tools'), sub('examples/m*'))
        self.assertEqual([x['path'] for x in expanded], ['tools', 'examples/mid'])

    def test_a_glob_without_a_project_is_refused(self):
        with self.assertRaises(run.ExpandError):
            self.expand(sub('examples/nothing*'))
        with self.assertRaises(run.ExpandError):
            self.expand(sub('examples/note?'))

    def test_a_project_matched_twice_is_refused(self):
        with self.assertRaises(run.ExpandError):
            self.expand(sub('examples/alpha'), sub('examples/*'))


N1_CPUINFO = 'processor\t: 0\nBogoMIPS\t: 50.00\nFeatures\t: fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp cpuid asimdrdm lrcpc dcpop asimddp ssbs\nCPU implementer\t: 0x41\n'
N2_CPUINFO = 'processor\t: 0\nFeatures\t: fp asimd aes sha2 crc32 atomics fphp asimdhp cpuid asimdrdm jscvt fcma lrcpc dcpop sha3 sm3 sm4 asimddp sha512 sve asimdfhm dit uscat ilrcpc flagm ssbs sb paca pacg dcpodp sve2 i8mm bf16 dgh\nCPU implementer\t: 0x41\n'
X86_CPUINFO = 'processor\t: 0\nvendor_id\t: GenuineIntel\nflags\t: fpu vme de dit\n'
DARWIN = {'name': 'darwin-aarch64', 'isa': 'aarch64', 'os': 'darwin'}
X86 = {'name': 'linux-x86_64', 'isa': 'x86_64', 'os': 'linux'}


class Dit(unittest.TestCase):
    def mechanism(self, dit='required', test=True, leg_test=True, runner='', host=HOST, has_dit=False):
        config = {'dit': dit, 'test': test}
        return run.dit_mechanism(config, dict(leg(test=leg_test), runner=runner), host, has_dit)

    def test_cpuinfo_features_carry_the_flag(self):
        self.assertFalse(run.cpuinfo_has_dit(N1_CPUINFO))
        self.assertTrue(run.cpuinfo_has_dit(N2_CPUINFO))
        # only the aarch64 Features line counts, and only as a whole word
        self.assertFalse(run.cpuinfo_has_dit(X86_CPUINFO))
        self.assertFalse(run.cpuinfo_has_dit('Features\t: fp editor\n'))

    def test_not_required_is_none(self):
        self.assertEqual(self.mechanism(dit='none')[0], 'none')

    def test_no_tests_means_nothing_to_do(self):
        self.assertEqual(self.mechanism(test=False)[0], 'none')
        self.assertEqual(self.mechanism(leg_test=False)[0], 'none')

    def test_a_leg_with_its_own_runner_is_left_alone(self):
        path, reason = self.mechanism(runner='qemu-aarch64')
        self.assertEqual(path, 'runner')
        self.assertIn('qemu-aarch64', reason)

    def test_other_isas_run_natively(self):
        self.assertEqual(self.mechanism(host=X86)[0], 'native')

    def test_a_processor_with_the_mode_runs_natively(self):
        self.assertEqual(self.mechanism(has_dit=True)[0], 'native')
        self.assertEqual(self.mechanism(host=DARWIN, has_dit=True)[0], 'native')

    def test_a_linux_processor_without_the_mode_is_emulated(self):
        path, reason = self.mechanism()
        self.assertEqual(path, 'emulated')
        self.assertIn('qemu-aarch64 -cpu max', reason)

    def test_no_emulation_elsewhere(self):
        with self.assertRaises(run.DitError):
            self.mechanism(host=DARWIN)

    @unittest.skipUnless(os.name == 'posix', 'the wrapper is a shell script on a linux runner')
    def test_the_wrapper_runs_qemu_with_the_model(self):
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, RUNNER_TEMP=temp, PATH=temp + os.pathsep + os.environ['PATH'])
            with unittest.mock.patch.dict(os.environ, env):
                wrapper = Path(temp) / 'qemu-aarch64'
                wrapper.write_text('#!/bin/sh\necho "$@"\n')
                wrapper.chmod(0o755)
                path = run.dit_runner()
            self.assertTrue(path.startswith(temp))
            self.assertIn('exec qemu-aarch64 -cpu max "$@"', Path(path).read_text())
            self.assertTrue(os.access(path, os.X_OK))


LS_REMOTE = (
    'aaaa\trefs/tags/v3.0.1\n'
    'bbbb\trefs/tags/v5.8.0\n'
    'cccc\trefs/tags/v5.8.0^{}\n'
    'cccc\trefs/tags/v5.8.0-rc1^{}\n'
    'cccc\trefs/tags/nightly\n'
    'cccc\trefs/tags/nightly^{}\n'
    'dddd\trefs/tags/v5.9.0^{}\n'
)


class SubmoduleTags(unittest.TestCase):
    def test_an_annotated_tag_matches_by_its_peeled_line(self):
        self.assertEqual(run.release_tags(LS_REMOTE, 'cccc'), ['v5.8.0', 'v5.8.0-rc1'])

    def test_a_lightweight_tag_matches_directly(self):
        self.assertEqual(run.release_tags(LS_REMOTE, 'aaaa'), ['v3.0.1'])

    def test_the_tag_object_is_not_the_commit(self):
        self.assertEqual(run.release_tags(LS_REMOTE, 'bbbb'), [])

    def test_only_v_tags_are_releases(self):
        self.assertEqual(run.release_tags('cccc\trefs/tags/nightly\ncccc\trefs/tags/nightly^{}\n', 'cccc'), [])

    def test_submodule_status_gives_path_and_commit(self):
        status = (' 04076e3cc6be8c5f8b9f3cfb10a1f9d213d519c1 dep/std (v5.8.0)\n'
                  '-edda4a039c4a5c926323328d5a1a4c8eb25608a7 dep/mach\n'
                  '+1111111111111111111111111111111111111111 dep/std/dep/x (heads/main)\n')
        self.assertEqual(run.parse_submodule_status(status), [
            ('dep/std', '04076e3cc6be8c5f8b9f3cfb10a1f9d213d519c1'),
            ('dep/mach', 'edda4a039c4a5c926323328d5a1a4c8eb25608a7'),
            ('dep/std/dep/x', '1111111111111111111111111111111111111111')])
        self.assertEqual(run.parse_submodule_status(''), [])


if __name__ == '__main__':
    unittest.main()
