import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib


def mach(*args, cwd=None):
    command = [os.environ['MACH_COMPILER'], *args]
    print('+ mach ' + ' '.join(args), flush=True)
    subprocess.run(command, check=True, cwd=cwd)


def leg_args(leg, command):
    args = ['--target', leg['target']] if leg['target'] else []
    if command == 'test' and leg['runner']:
        args += ['--runner', leg['runner']]
    return args + leg[command + '-args']


def applies(sub, leg):
    if sub['legs'] and leg['name'] not in sub['legs']:
        return False
    return sub['tier'] == 'light' or leg['run-tier'] == 'heavy'


def host():
    info = subprocess.run([os.environ['MACH_COMPILER'], 'info'], capture_output=True, text=True, check=True).stdout
    return parse_host(info)


def parse_host(info):
    fields = dict(line.split(': ', 1) for line in info.splitlines() if ': ' in line)
    return {'name': fields['host'], 'isa': fields['isa'], 'os': fields['os']}


def manifest_problems(path, manifest, leg, profiles, host, tested):
    problems = []
    targets = manifest.get('target', {})
    if leg['target']:
        if leg['target'] not in targets:
            problems.append(path + ' declares no target ' + leg['target'] + ' for leg ' + leg['name'])
    # with no host match mach falls back to a default target, and the tests then
    # run a binary this host cannot execute. a build-only project may target
    # something else entirely, such as a spirv-only shader project.
    elif tested and targets and not any(t.get('isa') == host['isa'] and t.get('os') == host['os'] for t in targets.values()):
        problems.append(path + ' declares no target for the host ' + host['name'] + ' of leg ' + leg['name']
                        + '; declare one, give the leg a target, or skip the leg')
    declared = manifest.get('profile', {})
    for profile in profiles:
        if profile not in declared:
            problems.append(path + ' declares no profile ' + profile)
    return problems


def leg_manifests(leg, config):
    tests = leg['test']
    manifests = [(config['project'], tests and config['test'])]
    manifests += [(sub['path'], tests and sub['test']) for sub in config['subprojects']
                  if applies(sub, leg) and (sub['build'] or sub['test'])]
    return manifests


def check_manifests(leg, config):
    found = host()
    problems = []
    for path, tested in leg_manifests(leg, config):
        manifest_path = str(Path(path) / 'mach.toml')
        with open(manifest_path, 'rb') as handle:
            manifest = tomllib.load(handle)
        profiles = list(config['profiles'])
        if path == config['project'] and leg['primary'] and config['all-targets'] and 'release' not in profiles:
            profiles.append('release')
        problems += manifest_problems(manifest_path, manifest, leg, profiles, found, tested)
    for problem in problems:
        print('::error::' + problem)
    if problems:
        sys.exit(1)
    print('subprojects: ' + (', '.join(sub['path'] for sub in config['subprojects']) or 'none'))
    print('every manifest declares the targets and profiles leg ' + leg['name'] + ' uses')


GLOB = re.compile(r'[*?[]')


class ExpandError(ValueError):
    pass


def expand_subprojects(subprojects, root='.'):
    expanded = []
    for sub in subprojects:
        if not GLOB.search(sub['path']):
            expanded.append(sub)
            continue
        # a directory without a manifest is not a project, whatever the glob says.
        # posix separators keep every host's paths comparable with the literal ones.
        matches = sorted(match.relative_to(root).as_posix() for match in Path(root).glob(sub['path'])
                         if (match / 'mach.toml').is_file())
        if not matches:
            raise ExpandError('subproject glob ' + sub['path'] + ' matches no directory holding a mach.toml')
        expanded += [dict(sub, path=path) for path in matches]
    paths = [sub['path'] for sub in expanded]
    duplicates = sorted({path for path in paths if paths.count(path) > 1})
    if duplicates:
        raise ExpandError('subprojects matched more than once: ' + ', '.join(duplicates))
    return expanded


def skip(reason):
    print('skipped: ' + reason)


def export_env(leg, config):
    values = dict(leg['env'])
    values.update({
        'MACH_CI_LEG': leg['name'],
        'MACH_CI_TIER': leg['run-tier'],
        'MACH_CI_HEAVY': config['heavy'],
        'MACH_CI_PRIMARY': 'true' if leg['primary'] else 'false',
        'MACH_CI_TARGET': leg['target'],
        'MACH_CI_RUNNER': leg['runner'],
        'MACH_CI_PROJECT': config['project'],
        'MACH_CI_PROFILES': ' '.join(config['profiles']),
    })
    with open(os.environ['GITHUB_ENV'], 'a') as output:
        for key, value in values.items():
            if '\n' in value:
                raise ValueError('leg env ' + key + ' spans lines')
            output.write(key + '=' + value + '\n')
            print(key + '=' + value)
    mach('info')


def deps(leg, config):
    mach('dep', 'pull', config['project'])
    for sub in config['subprojects']:
        if not (sub['pull'] and applies(sub, leg)):
            continue
        if sub['clean-dep']:
            shutil.rmtree(Path(sub['path']) / 'dep', ignore_errors=True)
        mach('dep', 'pull', sub['path'])


def build(leg, config):
    for profile in config['profiles']:
        mach('build', config['project'], '--profile', profile, *leg_args(leg, 'build'))


def test(leg, config):
    if not config['test']:
        return skip('test is off for this repo')
    if not leg['test']:
        return skip('test is off for leg ' + leg['name'])
    for profile in config['profiles']:
        mach('test', config['project'], '--profile', profile, *leg_args(leg, 'test'))


def subprojects(leg, config):
    ran = False
    for sub in config['subprojects']:
        if not applies(sub, leg):
            continue
        for profile in config['profiles']:
            if sub['build']:
                mach('build', sub['path'], '--profile', profile, *leg_args(leg, 'build'))
                ran = True
            if sub['test'] and leg['test']:
                jobs = ['--jobs', str(sub['jobs'])] if sub['jobs'] else []
                mach('test', sub['path'], '--profile', profile, *jobs, *leg_args(leg, 'test'))
                ran = True
    if not ran:
        skip('no subproject builds or tests on leg ' + leg['name'])


def fmt(leg, config):
    if not config['fmt']:
        return skip('fmt is off for this repo')
    if not leg['primary']:
        return skip('fmt runs once, on the primary leg')
    # formatting is host independent, so every subproject is checked here
    # whatever legs and tier it builds on
    failed = []
    for path in [config['project']] + [sub['path'] for sub in config['subprojects'] if sub['fmt']]:
        try:
            mach('fmt', '--check', path)
        except subprocess.CalledProcessError:
            failed.append(path)
    if failed:
        print('::error::not formatted: ' + ', '.join(failed))
        sys.exit(1)


def all_targets(leg, config):
    if not config['all-targets']:
        return skip('all-targets is off for this repo')
    if not leg['primary']:
        return skip('all-targets runs once, on the primary leg')
    mach('build', config['project'], '--all-targets', '--profile', 'release')


PHASES = {
    'env': export_env,
    'manifests': check_manifests,
    'deps': deps,
    'build': build,
    'test': test,
    'subprojects': subprojects,
    'fmt': fmt,
    'all-targets': all_targets,
}


def main():
    leg = json.loads(os.environ['MACH_LIB_LEG'])
    config = json.loads(os.environ['MACH_LIB_CONFIG'])
    try:
        config['subprojects'] = expand_subprojects(config['subprojects'])
        PHASES[sys.argv[1]](leg, config)
    except ExpandError as error:
        print('::error::' + str(error))
        sys.exit(1)
    except subprocess.CalledProcessError as error:
        print('::error::' + ' '.join(error.cmd[1:]) + ' exited ' + str(error.returncode))
        sys.exit(1)


if __name__ == '__main__':
    main()
