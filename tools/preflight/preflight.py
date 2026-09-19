"""preflight every adopter of the family CI toolkit against a mach release and a toolkit ref.

run it before merging a dev to main pull request here. see README.md in this directory.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
import zipfile

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
LIB_JOB = 'mach-lib.yml@'


def remove_tree(path):
    """remove a scratch tree even where tools left it read-only, such as a go module cache"""
    def force(function, target, _):
        for entry in (Path(target).parent, Path(target)):
            if entry.exists() and not entry.is_symlink():
                entry.chmod(entry.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
        function(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=force)
    else:
        shutil.rmtree(path, onerror=force)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sh(*command, cwd=None, capture=True):
    result = subprocess.run(command, cwd=cwd, capture_output=capture, text=True)
    if result.returncode:
        raise RuntimeError(' '.join(command) + ' exited ' + str(result.returncode) + ': '
                           + ((result.stderr or '') + (result.stdout or '')).strip()[-400:])
    return result.stdout if capture else ''


def lib_inputs(workflow_text, plan):
    """the plan inputs one adopter's ci.yml gives its mach-lib job, as a pull request would see them"""
    workflow = yaml.safe_load(workflow_text)
    withs = [job.get('with') or {} for job in workflow['jobs'].values() if LIB_JOB in str(job.get('uses', ''))]
    if len(withs) != 1:
        raise ValueError('expected one mach-lib job, found ' + str(len(withs)))
    given = withs[0]

    # an expression resolves at run time; a pull request leaves every dispatch input unset
    def get(key, default):
        value = given.get(key, default)
        return default if isinstance(value, str) and '${{' in value else value

    def flag(key):
        value = get(key, True)
        return value if isinstance(value, bool) else str(value) == 'true'

    inputs = {
        'legs': get('legs', '') or json.dumps(plan.DEFAULT_LEGS),
        'extra-legs': get('extra-legs', '') or '[]',
        'skip-legs': get('skip-legs', '') or '[]',
        'light-legs': get('light-legs', '') or '[]',
        'heavy': 'none',
        'profiles': get('profiles', '') or '["debug", "release"]',
        'subprojects': get('subprojects', '') or '[]',
        'project': get('project', '.'),
        'deps': get('deps', 'pull'),
        'dit': get('dit', 'none'),
        'hooks-dir': get('hooks-dir', '.github/ci'),
        'test': flag('test'),
        'fmt': flag('fmt'),
        'all-targets': flag('all-targets'),
        'timeout-minutes': int(get('timeout-minutes', 40)),
    }
    extra = {'submodules': str(get('submodules', 'false')), 'mach-version': str(get('mach-version', ''))}
    return inputs, extra


def committed_dep_symlinks(ls_files):
    """paths git records as symlinks that are, or sit inside, a dep directory"""
    found = []
    for line in ls_files.splitlines():
        meta, _, path = line.partition('\t')
        if meta.split()[0] == '120000' and 'dep' in Path(path).parts:
            found.append(path)
    return found


def dep_symlinks(root):
    return sorted(str(path.relative_to(root)) for path in Path(root).rglob('dep')
                  if path.is_symlink() and '.git' not in path.parts)


class Toolkit:
    """the toolkit scripts at the ref under release"""

    def __init__(self, ref, work):
        self.dir = work / 'toolkit'
        if self.dir.exists():
            remove_tree(self.dir)
        self.dir.mkdir(parents=True)
        archive = subprocess.run(['git', 'archive', ref, '.github'], cwd=REPO, capture_output=True, check=True).stdout
        subprocess.run(['tar', '-x', '-C', str(self.dir)], input=archive, check=True)
        self.sha = sh('git', 'rev-parse', '--short', ref, cwd=REPO).strip()
        lib = self.dir / '.github/mach-lib'
        self.plan = load_module('plan', lib / 'plan.py')
        self.run_py = lib / 'run.py'
        self.run = load_module('run', self.run_py)
        self.verify = load_module('verify', self.dir / '.github/actions/seed-mach/verify.py')
        self.pin = (self.dir / '.github/actions/seed-mach/version').read_text().strip()


def local_host():
    machine = {'x86_64': 'x86_64', 'amd64': 'x86_64', 'aarch64': 'aarch64', 'arm64': 'aarch64'}[platform.machine().lower()]
    system = {'Linux': ('linux', 'tar.gz'), 'Darwin': ('darwin', 'tar.gz'), 'Windows': ('windows', 'zip')}[platform.system()]
    return machine + '-' + system[0], system[1]


def seed(toolkit, tag, work, org):
    """download the release for this host, verify it the way seed-mach does, and return the compiler"""
    host, ext = local_host()
    directory = work / 'seed'
    if directory.exists():
        remove_tree(directory)
    directory.mkdir(parents=True)
    metadata = json.loads(sh('gh', 'api', 'repos/' + org + '/mach/releases/tags/' + tag))
    tag, asset = toolkit.verify.select_release(metadata, tag, host, ext)
    sh('gh', 'release', 'download', tag, '-R', org + '/mach', '-p', asset, '-p', 'SHA256SUMS', '-D', str(directory))
    digest = toolkit.verify.verify_archive(directory / asset, (directory / 'SHA256SUMS').read_text())
    name = 'mach.exe' if ext == 'zip' else 'mach'
    if ext == 'zip':
        with zipfile.ZipFile(directory / asset) as archive:
            archive.extract(name, directory)
    else:
        with tarfile.open(directory / asset) as archive:
            archive.extract(name, directory, filter='data')
    compiler = directory / name
    compiler.chmod(0o755)
    version = sh(str(compiler), 'info', '--version').strip()
    if 'v' + version != tag:
        raise RuntimeError('seed reports ' + version + ', not ' + tag)
    return compiler, asset + ' sha256 ' + digest


def workflow_text(org, name, branch):
    """the repo's ci.yml on the branch, or None when the repo has none; any other failure raises"""
    result = subprocess.run(['gh', 'api', '-H', 'Accept: application/vnd.github.raw',
                             'repos/' + org + '/' + name + '/contents/.github/workflows/ci.yml?ref=' + branch],
                            capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout
    if 'HTTP 404' in result.stderr:
        return None
    raise RuntimeError('could not read ' + name + ' ci.yml: ' + result.stderr.strip()[-400:])


def adopters(config):
    # a repo is only skipped when it has no ci.yml on the branch; a transient
    # failure raises rather than silently narrowing the preflight
    org, branch = config['org'], config['branch']
    names = sh('gh', 'repo', 'list', org, '-L', '500', '--no-archived', '--json', 'name', '--jq', '.[].name').split()
    found = {}
    for name in sorted(names):
        if name == '.github':
            continue
        text = workflow_text(org, name, branch)
        if text is not None and LIB_JOB in text:
            found[name] = text
    return found


def clone(org, name, branch, submodules, destination):
    if destination.exists():
        remove_tree(destination)
    sh('git', 'clone', '-q', '--depth', '1', '-b', branch, 'https://github.com/' + org + '/' + name, str(destination))
    # match actions/checkout: no submodules unless the caller asks, and recursion only when it says so
    if submodules in ('true', 'recursive'):
        sh('git', 'submodule', 'update', '-q', '--init', '--depth', '1',
           *(['--recursive'] if submodules == 'recursive' else []), cwd=destination)
    return sh('git', 'rev-parse', '--short', 'HEAD', cwd=destination).strip()


def check(toolkit, config, compiler, root, inputs):
    """the plan, fmt, manifests and dep-symlink checks for one adopter; returns problems"""
    problems = []
    for base in ('dev', 'main'):
        try:
            matrix, plan_config = toolkit.plan.plan(dict(inputs), base, str(root))
        except toolkit.plan.PlanError as error:
            problems.append('plan into ' + base + ': ' + str(error))
    if problems:
        return problems, []
    plan_config['subprojects'] = toolkit.run.expand_subprojects(plan_config['subprojects'], str(root))
    if plan_config['fmt']:
        for path in [plan_config['project']] + [sub['path'] for sub in plan_config['subprojects'] if sub['fmt']]:
            result = subprocess.run([str(compiler), 'fmt', '--check', path], cwd=root, capture_output=True, text=True)
            if result.returncode:
                problems.append('fmt ' + path + ': ' + (result.stdout + result.stderr).strip()[-200:])
    hosts = config['hosts']
    for entry in matrix['include']:
        leg = entry['leg']
        label = leg['runs-on'] if isinstance(leg['runs-on'], str) else leg['runs-on'][0]
        if label not in hosts:
            problems.append('leg ' + leg['name'] + ': runs-on ' + label + ' is not in preflight.toml [hosts]')
            continue
        host = dict(hosts[label], name=label)
        for path, tested in toolkit.run.leg_manifests(leg, plan_config):
            manifest = tomllib.loads((root / path / 'mach.toml').read_text())
            profiles = list(plan_config['profiles'])
            if path == plan_config['project'] and leg['primary'] and plan_config['all-targets'] and 'release' not in profiles:
                profiles.append('release')
            problems += ['leg ' + leg['name'] + ': ' + problem for problem in
                         toolkit.run.manifest_problems(path + '/mach.toml', manifest, leg, profiles, host, tested)]
    links = committed_dep_symlinks(sh('git', 'ls-files', '-s', cwd=root))
    problems += ['committed dep symlink ' + link for link in links]
    return problems, [e['leg']['name'] for e in matrix['include']]


def sample(toolkit, compiler, name, root, inputs, work, env_path):
    """the primary leg's phases and hooks, as mach-lib.yml runs them on a pull request into main"""
    matrix, plan_config = toolkit.plan.plan(dict(inputs), 'main', str(root))
    leg = next(e['leg'] for e in matrix['include'] if e['leg']['primary'])
    temp = work / 'runner' / name
    if temp.exists():
        remove_tree(temp)
    temp.mkdir(parents=True)
    (temp / 'env').write_text('')
    env = dict(os.environ, MACH_COMPILER=str(compiler), MACH_LIB_LEG=json.dumps(leg),
               MACH_LIB_CONFIG=json.dumps(plan_config), GITHUB_ENV=str(temp / 'env'),
               GITHUB_BASE_REF='main', RUNNER_TEMP=str(temp), RUNNER_OS=platform.system())
    if env_path:
        env['PATH'] = env_path + os.pathsep + env['PATH']
    log = (work / 'logs' / (name + '.log')).open('w')
    steps = []

    def step(label, command):
        exported = dict(line.split('=', 1) for line in (temp / 'env').read_text().splitlines() if '=' in line)
        log.write('== ' + label + '\n')
        log.flush()
        code = subprocess.run(command, cwd=root, env=dict(env, **exported), stdout=log, stderr=subprocess.STDOUT).returncode
        steps.append((label, code == 0))
        return code == 0

    script = [sys.executable, str(toolkit.run_py)]
    hooks, hooks_dir = plan_config['hooks'], plan_config['hooks-dir']
    ok = step('env', script + ['env']) and step('manifests', script + ['manifests']) and step('dit', script + ['dit'])
    setup = ok and 'setup.sh' in hooks
    if setup:
        ok = step('setup hook', ['bash', hooks_dir + '/setup.sh'])
    for phase in ('deps', 'build', 'test', 'subprojects', 'fmt', 'all-targets'):
        ok = ok and step(phase, script + [phase])
    if ok and 'verify.sh' in hooks:
        ok = step('verify hook', ['bash', hooks_dir + '/verify.sh'])
    if setup and 'teardown.sh' in hooks:
        ok = step('teardown hook', ['bash', hooks_dir + '/teardown.sh']) and ok
    log.close()
    links = dep_symlinks(root)
    if links:
        steps.append(('dep symlinks ' + ', '.join(links), False))
        ok = False
    return leg['name'], ok, steps


def main():
    config = tomllib.loads((HERE / 'preflight.toml').read_text())
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--mach', help='mach release tag to seed; default is the pin at --toolkit')
    parser.add_argument('--toolkit', default='HEAD', help='toolkit git ref under release (default HEAD)')
    parser.add_argument('--work', type=Path, help='scratch directory (default .wt/preflight-<tag>)')
    parser.add_argument('--repos', nargs='*', help='only these adopters')
    parser.add_argument('--no-sample', action='store_true', help='skip the end to end sample legs')
    parser.add_argument('--path', default='', help='directory prepended to PATH for sample hooks')
    parser.add_argument('--local', action='append', default=[], metavar='NAME=PATH',
                        help='use this checkout for the adopter instead of cloning its dev, reading ci.yml from it; '
                             'for proving a toolkit change on a branch or an edited ci.yml')
    args = parser.parse_args()

    pin = sh('git', 'show', args.toolkit + ':.github/actions/seed-mach/version', cwd=REPO).strip()
    tag = args.mach or pin
    work = (args.work or REPO / '.wt' / ('preflight-' + tag)).resolve()
    (work / 'logs').mkdir(parents=True, exist_ok=True)
    toolkit = Toolkit(args.toolkit, work)
    compiler, provenance = seed(toolkit, tag, work, config['org'])
    print('toolkit ' + args.toolkit + ' (' + toolkit.sha + '), pin ' + toolkit.pin)
    print('seed ' + tag + ': ' + provenance)

    local = {}
    for item in args.local:
        name, _, path = item.partition('=')
        if not (name and path):
            sys.exit('--local takes NAME=PATH, got ' + item)
        local[name] = Path(path).resolve()
    found = adopters(config)
    for name, path in local.items():
        found[name] = (path / '.github/workflows/ci.yml').read_text()
    excluded = config.get('exclude', {})
    for name in sorted(set(found) & set(excluded)):
        print('excluded ' + name + ': ' + excluded[name])
    names = [name for name in sorted(found) if name not in excluded]
    if args.repos:
        unknown = sorted(set(args.repos) - set(names))
        if unknown:
            sys.exit('not a checked adopter: ' + ', '.join(unknown))
        names = [name for name in names if name in args.repos]
    print('adopters: ' + str(len(names)))

    failed = []
    roots = {}
    for name in names:
        extra, head = {}, '?'
        try:
            inputs, extra = lib_inputs(found[name], toolkit.plan)
            if name in local:
                root = local[name]
                head = sh('git', 'rev-parse', '--short', 'HEAD', cwd=root).strip() + ' (local)'
            else:
                root = work / 'repos' / name
                head = clone(config['org'], name, config['branch'], extra['submodules'], root)
            roots[name] = (root, inputs)
            problems, legs = check(toolkit, config, compiler, root, inputs)
        except (RuntimeError, ValueError, OSError) as error:
            problems, legs = [str(error)], []
        pinned = ' mach-version=' + extra['mach-version'] if extra.get('mach-version') else ''
        print(('FAIL ' if problems else 'ok   ') + name + '@' + head + ' legs=' + ','.join(legs) + pinned)
        for problem in problems:
            print('     ' + problem)
        if problems:
            failed.append(name)

    if not args.no_sample:
        for name in list(config['sample']['repos']) + [name for name in local if name not in config['sample']['repos']]:
            if name not in roots or name in failed:
                continue
            root, inputs = roots[name]
            leg, ok, steps = sample(toolkit, compiler, name, root, inputs, work, args.path)
            print(('PASS ' if ok else 'FAIL ') + name + ' sample ' + leg + ': '
                  + ' '.join(label if passed else label.upper() + '!' for label, passed in steps))
            if not ok:
                print('     log: ' + str(work / 'logs' / (name + '.log')))
                failed.append(name + ' (sample)')

    print('failed: ' + (', '.join(failed) or 'none'))
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
