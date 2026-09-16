import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('gate', Path(__file__).with_name('gate.py'))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

WORKFLOW = '''
on: pull_request
jobs:
  lib:
    uses: ./x.yml
  extra:
    runs-on: ubuntu-latest
  gate:
    needs: [lib, extra]
'''


def needs(**results):
    return {name: {'result': result, 'outputs': {}} for name, result in results.items()}


class Gate(unittest.TestCase):
    jobs = gate.workflow_jobs(WORKFLOW)

    def test_success_and_skipped_pass(self):
        self.assertEqual(gate.verdict(needs(lib='success', extra='skipped'), self.jobs, 'gate'), [])

    def test_failure_and_cancelled_fail(self):
        for result in ('failure', 'cancelled'):
            self.assertEqual(gate.verdict(needs(lib='success', extra=result), self.jobs, 'gate'),
                             ['extra finished ' + result])

    def test_unknown_result_fails(self):
        self.assertEqual(gate.verdict(needs(lib='success', extra='neutral'), self.jobs, 'gate'),
                         ['extra finished neutral'])

    def test_unneeded_job_fails(self):
        self.assertEqual(gate.verdict(needs(lib='success'), self.jobs, 'gate'),
                         ['the gate does not need: extra'])

    def test_empty_needs_fails(self):
        self.assertIn('the gate needs no jobs, so it cannot fail', gate.verdict({}, {'gate'}, 'gate'))

    def test_misnamed_gate_fails(self):
        problems = gate.verdict(needs(lib='success', extra='success'), self.jobs | {'check'}, 'check')
        self.assertIn('the gate job must be named gate, not check', problems)


if __name__ == '__main__':
    unittest.main()
