import importlib.util
from pathlib import Path
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


if __name__ == '__main__':
    unittest.main()
