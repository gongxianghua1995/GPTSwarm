"""Check actual native graph delivery, feedback and failure-safe submission."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from swarm.environment.agents.swe_bench.mini_collaboration import build_fixed_swarm
from swarm.environment.agents.swe_bench.mini_runtime import MiniRuntime, digest, review_verdict, interrupted_handoff, permission_only_diff
from swarm.environment.agents.swe_bench.mini_checks import check_evidence


class FakeRuntime:
    def __init__(self, verdict='request_changes', fail_phase=None):
        self.calls = []
        self.events = []
        self.patch = ''
        self.verdict = verdict
        self.fail_phase = fail_phase

    async def export(self):
        return dict(patch=self.patch, sha256=digest(self.patch), excluded_paths=[])

    def event(self, event, **values):
        self.events.append((event, values))

    async def phase(self, phase, role, messages):
        self.calls.append((phase, [m['phase'] for m in messages]))
        if phase == 'implementation':
            self.patch = 'actual initial workspace patch'
        if phase == self.fail_phase:
            raise RuntimeError('worker failed after changing source')
        if phase == 'revision':
            self.patch = 'actual revised workspace patch'
        return dict(exit_status='Submitted', report='message from ' + phase, checks=[], usage=[],
                    patch_sha256=digest(self.patch),
                    verdict=self.verdict if phase == 'review' else 'approve' if phase == 'final_review' else None)


class CollaborationTest(unittest.TestCase):
    def run_graph(self, runtime):
        swarm = build_fixed_swarm('mock', runtime)
        graph = swarm.composite_graph
        answer = asyncio.run(graph.run({'messages': []}, max_tries=1, max_time=10))
        self.assertFalse(swarm.edge_optimize)
        self.assertFalse(swarm.node_optimize)
        self.assertEqual(swarm.potential_connections, [])
        self.assertEqual(len(swarm.used_agents), 3)
        self.assertTrue(all(n.outputs for n in graph.nodes.values()))
        return answer, graph

    def test_predecessor_messages_reach_reviewer_and_engineer_revision(self):
        runtime = FakeRuntime()
        answer, graph = self.run_graph(runtime)
        self.assertEqual([c[0] for c in runtime.calls],
                         ['analysis', 'implementation', 'review', 'revision', 'final_review'])
        received = dict(runtime.calls)
        self.assertIn('analysis', received['implementation'])
        self.assertIn('implementation', received['review'])
        self.assertIn('review', received['revision'])
        self.assertIn('revision', received['final_review'])
        self.assertEqual(answer, ['actual revised workspace patch'])
        messages = graph.decision_method.outputs[0]['messages']
        self.assertEqual(len({m['id'] for m in messages}), 5)

    def test_approval_skips_unnecessary_revision_only_for_same_patch(self):
        runtime = FakeRuntime(verdict='approve')
        answer, graph = self.run_graph(runtime)
        self.assertEqual([c[0] for c in runtime.calls], ['analysis', 'implementation', 'review'])
        self.assertEqual(answer, ['actual initial workspace patch'])
        self.assertEqual(graph.decision_method.outputs[0]['local_verdict'], 'approve')

    def test_role_failure_keeps_patch_and_handoff_for_following_roles(self):
        runtime = FakeRuntime(fail_phase='implementation')
        answer, graph = self.run_graph(runtime)
        messages = graph.decision_method.outputs[0]['messages']
        self.assertEqual(messages[1]['exit_status'], 'PhaseError')
        self.assertIn('implementation', dict(runtime.calls)['review'])
        self.assertEqual(answer, ['actual revised workspace patch'])

    def test_unverified_review_requests_revision(self):
        runtime = FakeRuntime(verdict='unverified')
        self.run_graph(runtime)
        self.assertIn('revision', dict(runtime.calls))

    def test_stale_approval_cannot_skip_revision(self):
        class ChangedAfterReview(FakeRuntime):
            def event(self, event, **values):
                super().event(event, **values)
                if event == 'message_sent' and values.get('phase') == 'review':
                    self.patch = 'unreviewed change'
        runtime = ChangedAfterReview(verdict='approve')
        self.run_graph(runtime)
        self.assertIn('revision', dict(runtime.calls))

    def test_verdict_is_explicit_and_requires_submission(self):
        self.assertEqual(review_verdict('Not APPROVE', 'Submitted'), 'unverified')
        self.assertEqual(review_verdict('VERDICT: APPROVE', 'PhaseTimeout'), 'unverified')
        self.assertEqual(review_verdict('VERDICT: APPROVE\nVERDICT: REQUEST_CHANGES', 'Submitted'), 'unverified')
        self.assertEqual(review_verdict('VERDICT: REQUEST_CHANGES\nFix negative days.', 'Submitted'), 'request_changes')

    def test_timeout_preserves_findings_without_turning_them_into_approval(self):
        observation = dict(command='cat source.py', returncode=0, output='source context')
        report = interrupted_handoff('Analyst', 'PhaseTimeout', [observation], 'Change the lookahead.')
        self.assertIn('Change the lookahead.', report)
        self.assertIn('source context', report)
        self.assertIn('incomplete handoff', report)
        self.assertEqual(review_verdict(report, 'PhaseTimeout'), 'unverified')

    def test_zero_unittest_cases_are_not_passing_checks(self):
        evidence = check_evidence('swe-check python -m unittest',
                                  {'returncode': 0, 'output': 'Ran 0 tests in 0.000s\n\nOK\n'})
        self.assertEqual(evidence['status'], 'no_tests')

    def test_django_runner_checks_are_recognized(self):
        evidence = check_evidence('python runtests.py utils_tests.test_dateparse',
                                  {'returncode': 0, 'output': 'Ran 12 tests in 0.003s\n\nOK\n'})
        self.assertEqual(evidence['status'], 'tests_passed')


class PhaseLifecycleTest(unittest.IsolatedAsyncioTestCase):
    def test_only_executable_bit_differences_are_accepted(self):
        blob = 'a' * 40
        raw = f':100644 100755 {blob} {blob} M\0file name.py\0'
        self.assertTrue(permission_only_diff(raw))
        self.assertFalse(permission_only_diff(raw.replace(blob, 'b'*40, 1)))
        self.assertFalse(permission_only_diff(raw.replace('100755', '120000')))
        self.assertFalse(permission_only_diff(raw.replace(' M\0', ' A\0')))
        self.assertFalse(permission_only_diff(raw[:-1]))
        self.assertFalse(permission_only_diff(''))

    async def test_timed_out_worker_delivers_partial_report_and_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            config = dict(task_seconds=1200, phases={'analysis': dict(seconds=180, steps=30, max_tokens=16384)},
                          model='mock', temperature=0.2, api_timeout=180, command_timeout=120)
            runtime = MiniRuntime(dict(base_commit='base', problem_statement='fix'), config, Path(directory))
            runtime.main = 'main'
            runtime.export = AsyncMock(return_value=dict(patch='', sha256=digest(''), excluded_paths=[]))
            runtime.start_workspace = AsyncMock(return_value='snapshot')
            runtime.stop_workspace = AsyncMock()
            runtime.shell = AsyncMock(return_value=dict(returncode=0, output='Implement the negative lookahead.'))

            async def worker(request, folder, seconds):
                self.assertGreater(seconds, 1000)  # shared task, not the 180s role target
                self.assertLessEqual(request['soft_seconds'], 180)
                event = dict(event='action', command='cat source.py', check=None,
                             result=dict(returncode=0, output='the actual source context'))
                (folder / 'events.jsonl').write_text(json.dumps(event) + '\n')
                raise asyncio.TimeoutError()

            runtime.run_worker = worker
            with patch('swarm.environment.agents.swe_bench.mini_runtime.asyncio.sleep', new=AsyncMock()):
                result = await runtime.phase('analysis', 'Analyst', [])
            self.assertEqual(result['exit_status'], 'PhaseTimeout')
            self.assertIn('negative lookahead', result['report'])
            self.assertIn('actual source context', result['report'])
            runtime.stop_workspace.assert_awaited_once_with('snapshot')

    async def test_inspection_edits_invalidate_an_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            config = dict(task_seconds=1200, phases={'review': dict(seconds=180, steps=30, max_tokens=16384)},
                          model='mock', temperature=0.2, api_timeout=180, command_timeout=120)
            runtime = MiniRuntime(dict(base_commit='base', problem_statement='fix'), config, Path(directory))
            clean = dict(patch='engineer patch', sha256=digest('engineer patch'), excluded_paths=[])
            dirty = dict(patch='reviewer edit', sha256=digest('reviewer edit'), excluded_paths=[])
            runtime.export = AsyncMock(side_effect=[clean, clean, dirty, clean])
            runtime.start_workspace = AsyncMock(return_value='snapshot')
            runtime.stop_workspace = AsyncMock()

            async def worker(request, folder, seconds):
                (folder / 'result.json').write_text(json.dumps(dict(exit_status='Submitted', submission='VERDICT: APPROVE')))

            runtime.run_worker = worker
            result = await runtime.phase('review', 'Reviewer', [])
            self.assertEqual(result['exit_status'], 'ReadOnlyViolation')
            self.assertEqual(result['verdict'], 'unverified')
            self.assertEqual(result['patch_sha256'], clean['sha256'])


if __name__ == '__main__':
    unittest.main()
