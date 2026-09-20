"""Evidence-gated review, distinct role prompts and real shell status capture."""
import copy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from swarm.environment.agents.swe_bench.mini_protocol import assess_review, parse_handoff
from swarm.environment.agents.swe_bench.mini_prompts import ROLE_SYSTEM, role_task
from swarm.environment.agents.swe_bench.mini_checks import CHECK_SCRIPT, check_evidence
from swarm.environment.agents.swe_bench.mini_policy import request_budget, inherited_review_criteria


def report():
    return dict(summary='Checked the fix', facts=[], assumptions=[], acceptance_criteria=['expected behavior'],
                changes=[], evidence=[dict(check_id='C1', kind='behavior', claim='assertions pass', criterion=0),
                                      dict(check_id='C2', kind='regression', claim='regression passes', criterion=0)],
                open_questions=[], findings=[], check_dispositions=[], verdict='APPROVE')


def check(identifier, **kwargs):
    return dict(id=identifier, command='test ' + identifier, scope='patched', wrapped=True,
                tree_before='tree', tree_after='tree', returncode=0, status='tests_passed', summary='OK', **kwargs)


class ProtocolTest(unittest.TestCase):
    def assess(self, value, checks):
        return assess_review(value, 'Submitted', checks, 'tree', ['expected behavior'])

    def test_good_review_has_two_distinct_current_tree_checks(self):
        self.assertEqual(self.assess(report(), [check('C1'), check('C2')]), ('approve', []))

    def test_missing_stale_or_unwrapped_evidence_cannot_approve(self):
        for field, value in [('tree_after', 'older'), ('wrapped', False), ('returncode', 1)]:
            evidence = check('C1'); evidence[field] = value
            self.assertEqual(self.assess(report(), [evidence, check('C2')])[0], 'unverified')
        self.assertEqual(self.assess(report(), [check('C1')])[0], 'unverified')

    def test_printed_approve_and_unresolved_failures_cannot_bypass_checks(self):
        self.assertIsNone(parse_handoff('VERDICT: APPROVE'))
        bad = check('C3'); bad.update(returncode=1, status='failure_observed', summary='AssertionError')
        self.assertEqual(self.assess(report(), [check('C1'), check('C2'), bad])[0], 'unverified')
        value = report(); value['open_questions'] = ['unresolved behavior']
        self.assertEqual(self.assess(value, [check('C1'), check('C2')])[0], 'unverified')

    def test_preexisting_failure_requires_identical_baseline_evidence(self):
        bad = check('C3'); bad.update(returncode=1, status='failure_observed', summary='AssertionError')
        baseline = copy.deepcopy(bad); baseline.update(id='B1', scope='baseline')
        value = report()
        value['check_dispositions'] = [dict(check_id='C3', reason='Fails identically at baseline', baseline_check_id='B1')]
        self.assertEqual(self.assess(value, [check('C1'), check('C2'), bad, baseline])[0], 'approve')
        baseline['summary'] = 'different failure'
        self.assertEqual(self.assess(value, [check('C1'), check('C2'), bad, baseline])[0], 'unverified')

    def test_only_invocation_errors_can_be_superseded_by_another_check(self):
        bad = check('C3'); bad.update(returncode=1, status='invocation_error')
        value = report(); value['check_dispositions'] = [dict(check_id='C3', reason='Corrected wrong test name', replacement_check_id='C2')]
        self.assertEqual(self.assess(value, [check('C1'), check('C2'), bad])[0], 'approve')
        bad['status'] = 'failure_observed'
        self.assertEqual(self.assess(value, [check('C1'), check('C2'), bad])[0], 'unverified')

    def test_independent_criteria_cannot_be_dropped_after_disclosure(self):
        value = report(); value['acceptance_criteria'] = ['different requirement']
        self.assertEqual(self.assess(value, [check('C1'), check('C2')])[0], 'unverified')
        self.assertEqual(assess_review(report(), 'Submitted', [check('C1'), check('C2')], 'tree', [])[0], 'unverified')

    def test_roles_have_distinct_system_instructions_and_reviewer_delays_peer_reports(self):
        self.assertEqual(len(set(ROLE_SYSTEM.values())), 3)
        peers = [dict(id='engineer', report='PEER_CLAIM_SENTINEL')]
        self.assertNotIn('PEER_CLAIM_SENTINEL', role_task('review', 'Reviewer', '/testbed', 'public issue', peers))
        self.assertIn('PEER_CLAIM_SENTINEL', role_task('implementation', 'Engineer', '/testbed', 'public issue', peers))
        self.assertIn('PEER_CLAIM_SENTINEL', role_task('final_review', 'Reviewer', '/testbed', 'public issue', peers))

    def test_request_deadline_does_not_shrink_at_reporting_boundary(self):
        for soft_remaining in [78.62, 70.84, 60, 45, 10, 0, -60]:
            budget = request_budget(900, 300, 60, soft_remaining=soft_remaining)
            self.assertTrue(budget['can_request'])
            self.assertEqual(budget['timeout'], 300)
            self.assertEqual(budget['reporting'], soft_remaining <= 60)
        for remaining in [49, 30, 15, 6]:
            budget = request_budget(remaining, 300, 60)
            self.assertTrue(budget['can_request'])
            self.assertEqual(budget['timeout'], remaining - 5)
        self.assertFalse(request_budget(5, 180, 60)['can_request'])
        self.assertFalse(request_budget(0, 180, 60)['can_request'])
        self.assertEqual(request_budget(300, 180, 60)['timeout'], 180)

    def test_final_review_inherits_only_reviewer_criteria_and_cannot_discard_them(self):
        messages = [dict(role='Reviewer', phase='review', initial_acceptance=['expected behavior']),
                    dict(role='Engineer', phase='revision', initial_acceptance=['weaker criterion'])]
        inherited = inherited_review_criteria(messages)
        self.assertEqual(inherited, ['expected behavior'])
        self.assertEqual(assess_review(report(), 'Submitted', [check('C1'), check('C2')], 'tree', inherited)[0], 'approve')
        value = report(); value['acceptance_criteria'] = ['weaker criterion']
        self.assertEqual(assess_review(value, 'Submitted', [check('C1'), check('C2')], 'tree', inherited)[0], 'unverified')
        self.assertEqual(inherited_review_criteria(messages[1:]), [])

    def test_shell_echo_does_not_mask_recorded_test_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / 'swe-check'; helper.write_text(CHECK_SCRIPT); helper.chmod(0o755)
            journal = Path(directory) / 'checks.jsonl'
            env = dict(os.environ, SWARM_CHECK_JOURNAL=str(journal),
                       PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
            command = ' '.join(map(shlex.quote, [str(helper), sys.executable, '-c', 'raise SystemExit(7)']))
            completed = subprocess.run(['bash', '-o', 'pipefail', '-c', command + ' | tail -1; echo done'],
                                       env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(completed.returncode, 0)
            saved = json.loads(journal.read_text())
            self.assertEqual(saved['returncode'], 7)
            self.assertEqual(check_evidence('swe-check command', saved)['status'], 'command_failed')

    def test_custom_assertion_success_and_invocation_failures_are_distinct(self):
        self.assertEqual(check_evidence('swe-check python /tmp/check.py', dict(returncode=0, output='3 assertions passed'))['status'], 'tests_passed')
        self.assertEqual(check_evidence('swe-check python -m unittest', dict(returncode=1, output='unittest.loader._FailedTest\nFAILED (errors=1)'))['status'], 'invocation_error')

    def test_reading_assertion_source_is_not_a_test_failure(self):
        self.assertIsNone(check_evidence('cat tests/test_example.py', dict(returncode=0, output='raise AssertionError("bad")')))


if __name__ == '__main__':
    unittest.main()
