import json
import os
from pathlib import Path
import re
import sys

# every host mach publishes a seed for. the linux x86_64 leg is the light tier,
# the native hosts are the heavy tier: qemu is compute evidence, not ABI evidence.
DEFAULT_LEGS = [
    {'name': 'x86_64-linux', 'runs-on': 'ubuntu-latest', 'tier': 'light'},
    {'name': 'aarch64-linux', 'runs-on': 'ubuntu-24.04-arm', 'tier': 'heavy'},
    {'name': 'x86_64-windows', 'runs-on': 'windows-latest', 'tier': 'heavy'},
    {'name': 'aarch64-darwin', 'runs-on': 'macos-15', 'tier': 'heavy'},
    {'name': 'x86_64-darwin', 'runs-on': 'macos-15-intel', 'tier': 'heavy'},
]

HOOKS = ('setup.sh', 'verify.sh', 'teardown.sh')
TIERS = ('light', 'heavy')
NAME = re.compile(r'[a-z0-9][a-z0-9_-]*')


class PlanError(ValueError):
    pass


def check_keys(kind, entry, required, optional):
    if not isinstance(entry, dict):
        raise PlanError(kind + ' must be an object: ' + json.dumps(entry))
    missing = [key for key in required if key not in entry]
    unknown = sorted(set(entry) - set(required) - set(optional))
    if missing:
        raise PlanError(kind + ' ' + json.dumps(entry) + ' is missing ' + ', '.join(missing))
    if unknown:
        raise PlanError(kind + ' ' + json.dumps(entry) + ' has unknown keys ' + ', '.join(unknown))


def check_type(kind, key, value, types):
    types = types if isinstance(types, tuple) else (types,)
    # bool is an int subclass, so an int field must refuse it by name
    if not isinstance(value, types) or (isinstance(value, bool) and bool not in types):
        raise PlanError(kind + ' ' + key + ' has the wrong type: ' + json.dumps(value))


def string_list(kind, key, value):
    check_type(kind, key, value, list)
    for item in value:
        check_type(kind, key, item, str)
    return value


def parse_list(text, name):
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise PlanError(name + ' is not JSON: ' + str(error))
    if not isinstance(value, list):
        raise PlanError(name + ' must be a JSON array')
    return value


def normalize_leg(entry, timeout):
    kind = 'leg'
    check_keys(kind, entry, ('name', 'runs-on'),
               ('tier', 'target', 'runner', 'build-args', 'test-args', 'apt', 'env', 'test', 'timeout'))
    check_type(kind, 'name', entry['name'], str)
    if not NAME.fullmatch(entry['name']):
        raise PlanError('leg name ' + entry['name'] + ' must be lowercase letters, digits, underscores and dashes')
    leg = {
        'name': entry['name'],
        'runs-on': entry['runs-on'],
        'tier': entry.get('tier', 'light'),
        'target': entry.get('target', ''),
        'runner': entry.get('runner', ''),
        'build-args': string_list(kind, 'build-args', entry.get('build-args', [])),
        'test-args': string_list(kind, 'test-args', entry.get('test-args', [])),
        'apt': string_list(kind, 'apt', entry.get('apt', [])),
        'env': entry.get('env', {}),
        'test': entry.get('test', True),
        'timeout': entry.get('timeout', timeout),
    }
    check_type(kind, 'runs-on', leg['runs-on'], (str, list))
    for key in ('tier', 'target', 'runner'):
        check_type(kind, key, leg[key], str)
    check_type(kind, 'env', leg['env'], dict)
    for value in leg['env'].values():
        check_type(kind, 'env', value, str)
    check_type(kind, 'test', leg['test'], bool)
    check_type(kind, 'timeout', leg['timeout'], int)
    if leg['tier'] not in TIERS:
        raise PlanError('leg ' + leg['name'] + ' tier must be light or heavy')
    if leg['runner'] and not leg['target']:
        raise PlanError('leg ' + leg['name'] + ' names a runner without a target')
    return leg


def normalize_subproject(entry, legs):
    kind = 'subproject'
    check_keys(kind, entry, ('path',),
               ('pull', 'build', 'test', 'fmt', 'clean-dep', 'jobs', 'legs', 'tier'))
    sub = {
        'path': entry['path'],
        'pull': entry.get('pull', True),
        'build': entry.get('build', False),
        'test': entry.get('test', True),
        'fmt': entry.get('fmt', True),
        'clean-dep': entry.get('clean-dep', False),
        'jobs': entry.get('jobs', 0),
        'legs': string_list(kind, 'legs', entry.get('legs', [])),
        'tier': entry.get('tier', 'light'),
    }
    check_type(kind, 'path', sub['path'], str)
    for key in ('pull', 'build', 'test', 'fmt', 'clean-dep'):
        check_type(kind, key, sub[key], bool)
    check_type(kind, 'jobs', sub['jobs'], int)
    check_type(kind, 'tier', sub['tier'], str)
    if sub['tier'] not in TIERS:
        raise PlanError('subproject ' + sub['path'] + ' tier must be light or heavy')
    if (sub['build'] or sub['test']) and not sub['pull']:
        raise PlanError('subproject ' + sub['path'] + ' builds or tests without pulling its dependencies')
    unknown = sorted(set(sub['legs']) - legs)
    if unknown:
        raise PlanError('subproject ' + sub['path'] + ' names unknown legs ' + ', '.join(unknown))
    # a recursive glob would descend into dep/ and out/ trees that hold manifests of their own
    if '**' in sub['path']:
        raise PlanError('subproject ' + sub['path'] + ' uses **; name each level with *')
    sub['path'] = os.path.normpath(sub['path'])
    return sub


def check_hooks(root, hooks_dir):
    directory = Path(root) / hooks_dir
    if not directory.is_dir():
        return []
    found = sorted(path.name for path in directory.iterdir())
    unknown = [name for name in found if name not in HOOKS]
    if unknown:
        raise PlanError(hooks_dir + ' is reserved for ' + ', '.join(HOOKS) + ' and also holds ' + ', '.join(unknown))
    # teardown runs only after setup was attempted, so alone it would never run
    if 'teardown.sh' in found and 'setup.sh' not in found:
        raise PlanError(hooks_dir + ' has teardown.sh without setup.sh')
    return found


def plan(inputs, base_ref, root):
    timeout = inputs['timeout-minutes']
    legs = [normalize_leg(entry, timeout)
            for entry in parse_list(inputs['legs'], 'legs') + parse_list(inputs['extra-legs'], 'extra-legs')]
    names = [leg['name'] for leg in legs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise PlanError('duplicate leg names ' + ', '.join(duplicates))
    skip = string_list('input', 'skip-legs', parse_list(inputs['skip-legs'], 'skip-legs'))
    light = string_list('input', 'light-legs', parse_list(inputs['light-legs'], 'light-legs'))
    for name, selected in (('skip-legs', skip), ('light-legs', light)):
        unknown = sorted(set(selected) - set(names))
        if unknown:
            raise PlanError(name + ' names unknown legs ' + ', '.join(unknown))
    for leg in legs:
        if leg['name'] in light:
            leg['tier'] = 'light'

    # the manifests decide which profiles exist, and each leg checks them there
    profiles = string_list('input', 'profiles', parse_list(inputs['profiles'], 'profiles'))
    if not profiles:
        raise PlanError('profiles is empty')
    for profile in profiles:
        if not NAME.fullmatch(profile):
            raise PlanError('profile name ' + json.dumps(profile) + ' must be lowercase letters, digits, underscores and dashes')
    subprojects = [normalize_subproject(entry, set(names))
                   for entry in parse_list(inputs['subprojects'], 'subprojects')]
    paths = [sub['path'] for sub in subprojects]
    duplicates = sorted({path for path in paths if paths.count(path) > 1})
    if duplicates:
        raise PlanError('duplicate subproject paths ' + ', '.join(duplicates))

    # a dispatch names heavy work by leg name, or by a name the caller's own
    # jobs and hooks understand; `all` and a pull request into main mean all of it
    selection = [name for name in re.split(r'[\s,]+', inputs['heavy']) if name and name != 'none']
    full = base_ref == 'main' or 'all' in selection
    tier = 'heavy' if full else 'light'
    heavy = 'all' if full else ','.join(selection)

    # a pull request into dev runs only light legs, so without one it would build
    # nothing and gate would pass. the primary leg, and with it fmt and
    # all-targets, is always a light leg
    if not any(leg['tier'] == 'light' and leg['name'] not in skip for leg in legs):
        raise PlanError('no light leg runs, so a pull request into dev would build nothing; '
                        'add a light leg, promote one with light-legs, or skip fewer legs')

    included = [leg for leg in legs if leg['name'] not in skip and
                (leg['tier'] == 'light' or full or leg['name'] in selection)]
    primary = next(leg['name'] for leg in included if leg['tier'] == 'light')
    for leg in included:
        leg['primary'] = leg['name'] == primary
        leg['run-tier'] = 'heavy' if full or leg['name'] in selection else 'light'

    config = {
        'project': inputs['project'],
        'profiles': profiles,
        'test': inputs['test'],
        'fmt': inputs['fmt'],
        'all-targets': inputs['all-targets'],
        'subprojects': subprojects,
        'hooks': check_hooks(root, inputs['hooks-dir']),
        'hooks-dir': inputs['hooks-dir'],
        'tier': tier,
        'heavy': heavy,
    }
    return {'include': [{'leg': leg} for leg in included]}, config


def boolean(text):
    if text not in ('true', 'false'):
        raise PlanError('expected true or false, got ' + text)
    return text == 'true'


def main():
    env = os.environ
    inputs = {
        'legs': env['PLAN_LEGS'] or json.dumps(DEFAULT_LEGS),
        'extra-legs': env['PLAN_EXTRA_LEGS'] or '[]',
        'skip-legs': env['PLAN_SKIP_LEGS'] or '[]',
        'light-legs': env['PLAN_LIGHT_LEGS'] or '[]',
        'heavy': env['PLAN_HEAVY'],
        'profiles': env['PLAN_PROFILES'],
        'subprojects': env['PLAN_SUBPROJECTS'] or '[]',
        'project': env['PLAN_PROJECT'],
        'hooks-dir': env['PLAN_HOOKS_DIR'],
        'test': boolean(env['PLAN_TEST']),
        'fmt': boolean(env['PLAN_FMT']),
        'all-targets': boolean(env['PLAN_ALL_TARGETS']),
        'timeout-minutes': int(env['PLAN_TIMEOUT']),
    }
    try:
        matrix, config = plan(inputs, env.get('GITHUB_BASE_REF', ''), sys.argv[1])
    except PlanError as error:
        print('::error::' + str(error))
        sys.exit(1)
    print(json.dumps({'matrix': matrix, 'config': config}, indent=2))
    with open(env['GITHUB_OUTPUT'], 'a') as output:
        output.write('matrix=' + json.dumps(matrix) + '\n')
        output.write('config=' + json.dumps(config) + '\n')


if __name__ == '__main__':
    main()
