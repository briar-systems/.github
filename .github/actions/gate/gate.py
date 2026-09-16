import json
import os
import sys

import yaml

PASSING = ('success', 'skipped')


def workflow_jobs(text):
    return set(yaml.safe_load(text)['jobs'])


def verdict(needs, jobs, gate):
    problems = []
    if gate != 'gate':
        problems.append('the gate job must be named gate, not ' + gate)
    if not needs:
        problems.append('the gate needs no jobs, so it cannot fail')
    missing = sorted(jobs - {gate} - set(needs))
    if missing:
        problems.append('the gate does not need: ' + ', '.join(missing))
    for name, job in sorted(needs.items()):
        result = job['result']
        if result not in PASSING:
            problems.append(name + ' finished ' + result)
    return problems


def main():
    needs = json.loads(os.environ['NEEDS'])
    gate = os.environ['GATE_JOB']
    with open(sys.argv[1]) as handle:
        jobs = workflow_jobs(handle.read())
    for name, job in sorted(needs.items()):
        print(name + ': ' + job['result'])
    problems = verdict(needs, jobs, gate)
    for problem in problems:
        print('::error::' + problem)
    sys.exit(1 if problems else 0)


if __name__ == '__main__':
    main()
