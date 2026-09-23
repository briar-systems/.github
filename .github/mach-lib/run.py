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


class SelectionError(RuntimeError):
    pass


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
            output.write(key + '=' + value + '\n')
            print(key + '=' + value)
    mach('info')


# the qemu processor model with FEAT_DIT; -cpu max is every feature qemu implements
DIT_CPU = 'max'


def cpuinfo_has_dit(cpuinfo):
    # the kernel prints Features from the same HWCAP word std's start reads
    for line in cpuinfo.splitlines():
        key, _, value = line.partition(':')
        if key.strip() == 'Features':
            return 'dit' in value.split()
    return False


class DitError(ValueError):
    pass


def dit_mechanism(config, leg, host, has_dit):
    """how the leg's tests get FEAT_DIT: (path, reason), path is none, native, runner or emulated"""
    if config['dit'] != 'required':
        return 'none', 'the project does not require DIT'
    if not (config['test'] and leg['test']):
        return 'none', 'no tests run on leg ' + leg['name']
    if leg['runner']:
        return 'runner', 'leg ' + leg['name'] + ' tests through ' + leg['runner'] + ', which decides the processor model'
    if host['isa'] != 'aarch64':
        return 'native', 'DIT is an aarch64 mode, ' + host['isa'] + ' tests run natively'
    if has_dit:
        return 'native', 'this ' + host['name'] + ' processor has FEAT_DIT, tests run natively'
    if host['os'] == 'linux':
        return 'emulated', 'this ' + host['name'] + ' processor lacks FEAT_DIT, tests run under qemu-aarch64 -cpu ' + DIT_CPU
    raise DitError('this ' + host['name'] + ' processor lacks FEAT_DIT and the toolkit has no emulation for ' + host['os'])


def host_has_dit(host):
    if host['isa'] != 'aarch64':
        return False
    if host['os'] == 'linux':
        return cpuinfo_has_dit(Path('/proc/cpuinfo').read_text())
    if host['os'] == 'darwin':
        sysctl = subprocess.run(['sysctl', '-n', 'hw.optional.arm.FEAT_DIT'], capture_output=True, text=True)
        return sysctl.stdout.strip() == '1'
    return False


def dit_runner():
    # a wrapper, because --runner takes a command with no arguments
    if not shutil.which('qemu-aarch64'):
        subprocess.run(['sudo', 'apt-get', 'update'], check=True)
        subprocess.run(['sudo', 'apt-get', 'install', '-y', 'qemu-user'], check=True)
    directory = Path(os.environ['RUNNER_TEMP']) / 'mach-lib-dit'
    directory.mkdir(exist_ok=True)
    wrapper = directory / 'qemu-aarch64-dit'
    wrapper.write_text('#!/bin/sh\nexec qemu-aarch64 -cpu ' + DIT_CPU + ' "$@"\n')
    wrapper.chmod(0o755)
    return str(wrapper)


def dit(leg, config):
    found = host()
    path, reason = dit_mechanism(config, leg, found, host_has_dit(found))
    print('dit: ' + path + ', ' + reason)
    values = {'MACH_CI_DIT': path}
    if path == 'emulated':
        values['MACH_LIB_DIT_RUNNER'] = dit_runner()
    with open(os.environ['GITHUB_ENV'], 'a') as output:
        for key, value in values.items():
            output.write(key + '=' + value + '\n')
            print(key + '=' + value)


RELEASE_TAG = re.compile(r'refs/tags/(v\d+\.\d+\.\d+[^^]*?)(\^\{\})?')


def release_tags(ls_remote, commit):
    """the v tags at `commit` in `git ls-remote --tags` output"""
    lines = []
    for line in ls_remote.splitlines():
        sha, _, ref = line.partition('\t')
        match = RELEASE_TAG.fullmatch(ref)
        if match:
            lines.append((match.group(1), bool(match.group(2)), sha))
    # an annotated tag's plain line is the tag object, its peeled line is the commit
    annotated = {tag for tag, peeled, _ in lines if peeled}
    return [tag for tag, peeled, sha in lines if sha == commit and (peeled or tag not in annotated)]


def parse_submodule_status(text):
    """(path, pinned commit) per line of `git submodule status`"""
    found = []
    for line in text.splitlines():
        # the first column is a state mark or a space, then the commit and the path
        fields = line[1:].split()
        if len(fields) >= 2:
            found.append((fields[1], fields[0]))
    return found


def git(*args, cwd=None):
    return subprocess.run(['git', *args], check=True, cwd=cwd, capture_output=True, text=True).stdout


def submodule_tags(leg, config):
    # a shallow checkout carries no tags, and a dependency selected by version
    # verifies against the release tag on its pinned commit, read from the
    # checkout's own refs. fetch exactly that tag for every submodule
    if config['submodules'] == 'false':
        return skip('the checkout has no submodules')
    recursive = ['--recursive'] if config['submodules'] == 'recursive' else []
    for path, commit in parse_submodule_status(git('submodule', 'status', *recursive)):
        tags = release_tags(git('ls-remote', '--tags', 'origin', cwd=path), commit)
        for tag in tags:
            git('fetch', '--depth=1', '--no-tags', 'origin', 'tag', tag, cwd=path)
        print('submodule ' + path + ' at ' + commit[:7] + (' is release ' + ', '.join(tags) if tags else ' carries no release tag'))


def resolve_deps(path, mode):
    # pull realizes committed pins; update resolves version ranges to the releases they select
    if mode == 'pull':
        mach('dep', 'pull', path)
    if mode == 'update':
        mach('dep', 'update', path, '--all')


def deps(leg, config):
    resolve_deps(config['project'], config['deps'])
    for sub in config['subprojects']:
        if not (sub['deps'] != 'none' and applies(sub, leg)):
            continue
        if sub['clean-dep']:
            shutil.rmtree(Path(sub['path']) / 'dep', ignore_errors=True)
        resolve_deps(sub['path'], sub['deps'])


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
        for selection in config['test-selections']:
            try:
                mach('test', config['project'], *selection, '--profile', profile, *leg_args(leg, 'test'))
            except subprocess.CalledProcessError as error:
                raise SelectionError('test selection ' + ' '.join(selection) + ' failed in profile ' + profile
                                     + ': ' + ' '.join(error.cmd[1:]) + ' exited ' + str(error.returncode))


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
    'submodule-tags': submodule_tags,
    'env': export_env,
    'manifests': check_manifests,
    'dit': dit,
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
    # the dit phase exports a runner when this processor lacks a mode the tests need
    leg['runner'] = leg['runner'] or os.environ.get('MACH_LIB_DIT_RUNNER', '')
    try:
        config['subprojects'] = expand_subprojects(config['subprojects'])
        PHASES[sys.argv[1]](leg, config)
    except (ExpandError, DitError, SelectionError) as error:
        print('::error::' + str(error))
        sys.exit(1)
    except subprocess.CalledProcessError as error:
        print('::error::' + ' '.join(error.cmd[1:]) + ' exited ' + str(error.returncode))
        sys.exit(1)


if __name__ == '__main__':
    main()
