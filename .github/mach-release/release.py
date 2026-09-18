import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tomllib

import yaml

SEMVER = re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?')
RELEASE_WORKFLOWS = ('briar-systems/.github/.github/workflows/mach-release.yml@',
                     # the toolkit's own rehearsal calls its working copy
                     './.github/workflows/mach-release.yml')
CI_WORKFLOW = './.github/workflows/ci.yml'


class ReleaseError(ValueError):
    pass


def project_version(manifest_text):
    try:
        version = tomllib.loads(manifest_text)['project']['version']
    except (KeyError, TypeError):
        raise ReleaseError('mach.toml has no [project] version')
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        raise ReleaseError('mach.toml version ' + json.dumps(version) + ' is not semver')
    return version


def resolve_tag(version, ref_type, ref_name, run_id):
    """the tag this run releases, and whether it only rehearses"""
    if ref_type == 'tag':
        if ref_name != 'v' + version:
            raise ReleaseError('tag ' + ref_name + ' does not match mach.toml version ' + version)
        return ref_name, False
    # a dispatch rehearses under a tag name that can never be pushed as a real release
    return 'v' + version + '-rehearsal.' + run_id, True


def changelog_notes(text, version):
    """the body of the `## [version]` section, which must exist and say something"""
    heading = re.compile(r'^## \[' + re.escape(version) + r'\](?:\s.*)?$')
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if heading.match(line)]
    if len(starts) != 1:
        raise ReleaseError('the changelog has ' + str(len(starts)) + ' "## [' + version + ']" sections, expected one')
    body = []
    for line in lines[starts[0] + 1:]:
        if line.startswith('## ') or re.match(r'^\[[^\]]+\]:\s', line):
            break
        body.append(line)
    notes = '\n'.join(body).strip()
    if not notes:
        raise ReleaseError('the changelog section for ' + version + ' is empty')
    return notes + '\n'


def expected_assets(spec, version):
    try:
        names = json.loads(spec) if spec.strip() else []
    except json.JSONDecodeError as error:
        raise ReleaseError('assets is not JSON: ' + str(error))
    if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
        raise ReleaseError('assets must be a JSON array of file names')
    names = [name.replace('{version}', version) for name in names]
    for name in names:
        if '/' in name or '\\' in name or name in ('.', '..'):
            raise ReleaseError('asset ' + name + ' must be a plain file name')
        if name == 'SHA256SUMS':
            raise ReleaseError('SHA256SUMS is generated; do not list it')
    if len(set(names)) != len(names):
        raise ReleaseError('assets lists a name twice')
    return names


def collect_assets(directory, names, checksums):
    """the files to upload: exactly the declared assets, plus SHA256SUMS over them"""
    directory = Path(directory)
    found = sorted(p.name for p in directory.iterdir() if p.is_file()) if directory.is_dir() else []
    missing = sorted(set(names) - set(found))
    extra = sorted(set(found) - set(names))
    if missing:
        raise ReleaseError('the build produced no ' + ', '.join(missing))
    if extra:
        raise ReleaseError('the build produced undeclared assets ' + ', '.join(extra))
    files = sorted(names)
    if names and checksums:
        lines = [hashlib.sha256((directory / n).read_bytes()).hexdigest() + '  ' + n for n in files]
        (directory / 'SHA256SUMS').write_text('\n'.join(lines) + '\n')
        files.append('SHA256SUMS')
    return files


def version_key(version):
    major, minor, patch, pre = SEMVER.fullmatch(version).groups()
    return int(major), int(minor), int(patch)


def make_latest(version, tag, releases):
    """latest only for a stable release no lower than every published stable release"""
    if SEMVER.fullmatch(version).group(4):
        return False
    stable = []
    for release in releases:
        if release['draft'] or release['prerelease'] or release['tag_name'] == tag:
            continue
        match = SEMVER.fullmatch(release['tag_name'][1:]) if release['tag_name'].startswith('v') else None
        if match and not match.group(4):
            stable.append(version_key(release['tag_name'][1:]))
    return all(version_key(version) >= other for other in stable)


def existing_release(tag, sha, releases):
    """what publish does about releases already carrying the tag: create, or done when one is published at this commit"""
    matching = [r for r in releases if r['tag_name'] == tag]
    if not matching:
        return 'create'
    published = [r for r in matching if not r['draft']]
    if len(matching) > 1 or not published:
        raise ReleaseError('a draft for ' + tag + ' already exists; a parallel run may still be publishing, '
                           'else delete the draft and re-run')
    release = published[0]
    if release.get('target_commitish') != sha:
        raise ReleaseError(tag + ' is already published from ' + str(release.get('target_commitish'))
                           + ', not ' + sha + '; release a new version instead')
    return 'done'


def caller_problems(workflow_text):
    """the caller's release workflow must gate publish on its full CI and on every other job"""
    workflow = yaml.safe_load(workflow_text) or {}
    jobs = workflow.get('jobs') or {}

    def stage(job):
        uses = str(job.get('uses', ''))
        return (job.get('with') or {}).get('stage') if uses.startswith(RELEASE_WORKFLOWS) else None

    def needs(job):
        value = job.get('needs', [])
        return {value} if isinstance(value, str) else set(value)

    problems = []
    concurrency = workflow.get('concurrency')
    group = concurrency.get('group', '') if isinstance(concurrency, dict) else ''
    cancel = concurrency.get('cancel-in-progress', False) if isinstance(concurrency, dict) else False
    # one tag push can be delivered twice; a group on the ref makes the second run wait for the first
    if 'github.ref' not in str(group) or cancel is not False:
        problems.append('the workflow must set concurrency to a group on github.ref with cancel-in-progress: false')
    publishers = [name for name, job in jobs.items() if stage(job) == 'publish']
    if len(publishers) != 1:
        return ['the workflow must have exactly one mach-release publish job, found ' + str(len(publishers))]
    publish = publishers[0]
    verifiers = [name for name, job in jobs.items() if stage(job) == 'verify']
    if len(verifiers) != 1:
        problems.append('the workflow must have exactly one mach-release verify job, found ' + str(len(verifiers)))
    full_ci = [name for name, job in jobs.items() if job.get('uses') == CI_WORKFLOW
               and str((job.get('with') or {}).get('heavy')) == 'all']
    if not full_ci:
        problems.append('no job calls ' + CI_WORKFLOW + ' with heavy: all')
    missing = sorted(set(jobs) - {publish} - needs(jobs[publish]))
    if missing:
        problems.append('the publish job does not need: ' + ', '.join(missing))
    for name, job in jobs.items():
        if name not in verifiers and name != publish and not (needs(job) & set(verifiers)):
            problems.append('job ' + name + ' does not need the verify job, so it runs before the tag is checked')
    return problems


def output(**values):
    with open(os.environ['GITHUB_OUTPUT'], 'a') as handle:
        for key, value in values.items():
            handle.write(key + '=' + str(value) + '\n')
            print(key + '=' + str(value))


def main():
    env = os.environ
    mode = sys.argv[1]
    if mode == 'verify':
        project, changelog, notes_path = sys.argv[2:]
        version = project_version((Path(project) / 'mach.toml').read_text())
        tag, rehearsal = resolve_tag(version, env['GITHUB_REF_TYPE'], env['GITHUB_REF_NAME'], env['GITHUB_RUN_ID'])
        notes = changelog_notes(Path(changelog).read_text(), version)
        expected_assets(env['RELEASE_ASSETS'], version)
        Path(notes_path).write_text(notes)
        prerelease = bool(SEMVER.fullmatch(version).group(4))
        output(version=version, tag=tag, rehearsal=str(rehearsal).lower(), prerelease=str(prerelease).lower())
        print(notes)
    elif mode == 'caller':
        problems = caller_problems(Path(sys.argv[2]).read_text())
        for problem in problems:
            print('::error::' + problem)
        if problems:
            sys.exit(1)
        print('publish is gated on the verify job, full CI and every other job')
    elif mode == 'assets':
        directory, version, list_path = sys.argv[2:]
        names = expected_assets(env['RELEASE_ASSETS'], version)
        files = collect_assets(directory, names, env['RELEASE_CHECKSUMS'] == 'true')
        Path(list_path).write_text(''.join(f + '\n' for f in files))
        print('assets: ' + (', '.join(files) or 'none'))
    elif mode == 'existing':
        tag, sha, releases_path = sys.argv[2:]
        action = existing_release(tag, sha, json.loads(Path(releases_path).read_text()))
        output(action=action)
        if action == 'done':
            print(tag + ' is already published at ' + sha + '; nothing to do')
    elif mode == 'latest':
        version, tag, releases_path = sys.argv[2:]
        latest = make_latest(version, tag, json.loads(Path(releases_path).read_text()))
        output(latest=str(latest).lower())
    else:
        raise SystemExit('unknown mode ' + mode)


if __name__ == '__main__':
    try:
        main()
    except ReleaseError as error:
        print('::error::' + str(error))
        sys.exit(1)
