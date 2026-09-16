import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


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
    mach('fmt', '--check', config['project'])


def all_targets(leg, config):
    if not config['all-targets']:
        return skip('all-targets is off for this repo')
    if not leg['primary']:
        return skip('all-targets runs once, on the primary leg')
    mach('build', config['project'], '--all-targets', '--profile', 'release')


PHASES = {
    'env': export_env,
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
        PHASES[sys.argv[1]](leg, config)
    except subprocess.CalledProcessError as error:
        print('::error::' + ' '.join(error.cmd[1:]) + ' exited ' + str(error.returncode))
        sys.exit(1)


if __name__ == '__main__':
    main()
