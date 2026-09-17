import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('release', Path(__file__).with_name('release.py'))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

CHANGELOG = """# Changelog

## [Unreleased]

- pending work

## [1.2.0] - 2026-09-16

### Added

- a thing

## [1.1.0] - 2026-09-01

- older

[1.2.0]: https://example.invalid/compare
"""

STUB = """
on:
  push:
    tags: ['v*']
  workflow_dispatch:
jobs:
  verify:
    uses: briar-systems/.github/.github/workflows/mach-release.yml@main
    with:
      stage: verify
  ci:
    needs: verify
    uses: ./.github/workflows/ci.yml
    with:
      heavy: all
  publish:
    needs: [verify, ci]
    uses: briar-systems/.github/.github/workflows/mach-release.yml@main
    permissions:
      contents: write
    with:
      stage: publish
"""


def stable(tag, draft=False, prerelease=False):
    return {'tag_name': tag, 'draft': draft, 'prerelease': prerelease}


class Version(unittest.TestCase):
    def test_reads_the_project_version(self):
        self.assertEqual(release.project_version('[project]\nid = "x"\nversion = "1.2.0"\n'), '1.2.0')

    def test_refusals(self):
        for label, text in {
            'no version': '[project]\nid = "x"\n',
            'no project': 'version = "1.0.0"\n',
            'not semver': '[project]\nversion = "1.2"\n',
            'leading v': '[project]\nversion = "v1.2.0"\n',
            'not a string': '[project]\nversion = 1\n',
        }.items():
            with self.subTest(label), self.assertRaises(release.ReleaseError):
                release.project_version(text)


class Tag(unittest.TestCase):
    def test_a_matching_tag_releases(self):
        self.assertEqual(release.resolve_tag('1.2.0', 'tag', 'v1.2.0', '7'), ('v1.2.0', False))

    def test_a_mismatched_tag_is_refused(self):
        for tag in ('v1.2.1', '1.2.0', 'v1.2.0-rc.1'):
            with self.subTest(tag), self.assertRaises(release.ReleaseError):
                release.resolve_tag('1.2.0', 'tag', tag, '7')

    def test_a_branch_rehearses_under_a_prerelease_tag(self):
        self.assertEqual(release.resolve_tag('1.2.0', 'branch', 'dev', '7'), ('v1.2.0-rehearsal.7', True))


class Notes(unittest.TestCase):
    def test_takes_only_the_version_section(self):
        self.assertEqual(release.changelog_notes(CHANGELOG, '1.2.0'), '### Added\n\n- a thing\n')

    def test_the_last_section_stops_at_link_references(self):
        self.assertEqual(release.changelog_notes(CHANGELOG, '1.1.0'), '- older\n')

    def test_a_heading_without_a_date_counts(self):
        self.assertEqual(release.changelog_notes('## [2.0.0]\n\n- x\n', '2.0.0'), '- x\n')

    def test_refusals(self):
        for label, (text, version) in {
            'missing': (CHANGELOG, '1.3.0'),
            'prefix only': ('## [1.2.0-rc.1]\n- x\n', '1.2.0'),
            'empty': ('## [1.2.0]\n\n## [1.1.0]\n- x\n', '1.2.0'),
            'twice': ('## [1.2.0]\n- a\n## [1.2.0]\n- b\n', '1.2.0'),
        }.items():
            with self.subTest(label), self.assertRaises(release.ReleaseError):
                release.changelog_notes(text, version)


class Assets(unittest.TestCase):
    def test_names_take_the_version(self):
        self.assertEqual(release.expected_assets('["mls-{version}-x86_64-linux.tar.gz"]', '1.2.0'),
                         ['mls-1.2.0-x86_64-linux.tar.gz'])

    def test_empty_means_none(self):
        self.assertEqual(release.expected_assets('', '1.2.0'), [])

    def test_spec_refusals(self):
        for label, spec in {
            'not json': '[mls]',
            'not array': '{"a": 1}',
            'path': '["dist/mls"]',
            'checksums listed': '["SHA256SUMS"]',
            'duplicate': '["a", "a"]',
            'empty name': '[""]',
        }.items():
            with self.subTest(label), self.assertRaises(release.ReleaseError):
                release.expected_assets(spec, '1.0.0')

    def test_collects_exactly_the_declared_set_with_checksums(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            (base / 'b.zip').write_bytes(b'bb')
            (base / 'a.tar.gz').write_bytes(b'aa')
            files = release.collect_assets(base, ['b.zip', 'a.tar.gz'], True)
            self.assertEqual(files, ['a.tar.gz', 'b.zip', 'SHA256SUMS'])
            want = ''.join(hashlib.sha256(data).hexdigest() + '  ' + name + '\n'
                           for name, data in (('a.tar.gz', b'aa'), ('b.zip', b'bb')))
            self.assertEqual((base / 'SHA256SUMS').read_text(), want)

    def test_no_assets_and_no_directory_is_fine(self):
        self.assertEqual(release.collect_assets('/nonexistent', [], True), [])

    def test_checksums_can_be_off(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'a').write_bytes(b'a')
            self.assertEqual(release.collect_assets(root, ['a'], False), ['a'])

    def test_missing_and_extra_assets_are_refused(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'a').write_bytes(b'a')
            (Path(root) / 'stray').write_bytes(b's')
            for names in (['a'], ['a', 'stray', 'b']):
                with self.subTest(names), self.assertRaises(release.ReleaseError):
                    release.collect_assets(root, names, True)


class Latest(unittest.TestCase):
    def test_the_highest_stable_is_latest(self):
        self.assertTrue(release.make_latest('1.2.0', 'v1.2.0', [stable('v1.1.0'), stable('v0.9.0')]))

    def test_a_backport_is_not_latest(self):
        self.assertFalse(release.make_latest('1.1.1', 'v1.1.1', [stable('v1.2.0')]))

    def test_a_prerelease_is_never_latest(self):
        self.assertFalse(release.make_latest('2.0.0-rc.1', 'v2.0.0-rc.1', []))

    def test_drafts_prereleases_and_odd_tags_do_not_count(self):
        releases = [stable('v9.0.0', draft=True), stable('v8.0.0', prerelease=True),
                    stable('v7.0.0-rc.1'), stable('nightly'), stable('7.0.0')]
        self.assertTrue(release.make_latest('1.0.0', 'v1.0.0', releases))

    def test_the_first_release_is_latest(self):
        self.assertTrue(release.make_latest('0.1.0', 'v0.1.0', []))


class Caller(unittest.TestCase):
    def test_the_stub_passes(self):
        self.assertEqual(release.caller_problems(STUB), [])

    def test_the_rehearsal_workflow_passes(self):
        text = (Path(__file__).parents[1] / 'workflows/release-rehearsal.yml').read_text()
        self.assertEqual(release.caller_problems(text), [])

    def test_publish_without_ci(self):
        text = STUB.replace('needs: [verify, ci]', 'needs: [verify]')
        self.assertEqual(release.caller_problems(text), ['the publish job does not need: ci'])

    def test_ci_that_is_not_full(self):
        text = STUB.replace('heavy: all', 'heavy: none')
        self.assertEqual(release.caller_problems(text), ['no job calls ./.github/workflows/ci.yml with heavy: all'])

    def test_a_build_job_must_follow_verify_and_gate_publish(self):
        text = STUB + '  build:\n    runs-on: ubuntu-latest\n    steps: []\n'
        self.assertEqual(release.caller_problems(text), [
            'the publish job does not need: build',
            'job build does not need the verify job, so it runs before the tag is checked',
        ])

    def test_stage_counts(self):
        no_verify = STUB.replace('stage: verify', 'stage: check')
        self.assertIn('the workflow must have exactly one mach-release verify job, found 0',
                      release.caller_problems(no_verify))
        no_publish = STUB.replace('stage: publish', 'stage: ship')
        self.assertEqual(release.caller_problems(no_publish),
                         ['the workflow must have exactly one mach-release publish job, found 0'])

    def test_another_workflow_of_the_same_name_does_not_count(self):
        text = STUB.replace('briar-systems/.github/.github/workflows/mach-release.yml@main',
                            'someone/fork/.github/workflows/mach-release.yml@main')
        self.assertEqual(release.caller_problems(text),
                         ['the workflow must have exactly one mach-release publish job, found 0'])


if __name__ == '__main__':
    unittest.main()
