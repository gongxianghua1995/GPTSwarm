"""Batch safeguards: distinguish hard quota failures and serialize domain queues."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.run_swebench_mini_batch import budget_exhaustion_log, domain_dependency_busy, run


class BatchGuardTests(unittest.TestCase):
    def test_transient_rate_limit_does_not_pause(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'analysis').mkdir()
            log = root / 'analysis/worker.log'
            log.write_text('RateLimitError: rate limit exceeded; retry later')
            self.assertIsNone(budget_exhaustion_log(root))
            log.write_text('RateLimitError: Budget has been exceeded! Key=private-value')
            self.assertEqual(budget_exhaustion_log(root), 'analysis/worker.log')

    def test_quota_codes_and_missing_logs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertIsNone(budget_exhaustion_log(root))
            (root / 'implementation').mkdir()
            for message in ['insufficient_quota', 'You exceeded your current quota']:
                (root / 'implementation/worker.log').write_text(message)
                self.assertEqual(budget_exhaustion_log(root), 'implementation/worker.log')

    def test_dependency_checks_only_the_requested_domain(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'status.json'
            state = dict(status='running', tasks={'a': dict(repo='ansible', status='pending'),
                                                 'b': dict(repo='flipt', status='finished')})
            p.write_text(json.dumps(state))
            self.assertTrue(domain_dependency_busy(p, 'ansible'))
            self.assertFalse(domain_dependency_busy(p, 'flipt'))
            state['tasks']['a']['status'] = 'finished'
            p.write_text(json.dumps(state))
            self.assertFalse(domain_dependency_busy(p, 'ansible'))

    def test_missing_or_interrupted_dependency_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'status.json'
            with self.assertRaises(RuntimeError):
                domain_dependency_busy(p, 'ansible')
            p.write_text(json.dumps(dict(status='interrupted', tasks={
                'a': dict(repo='ansible', status='interrupted')})))
            with self.assertRaises(RuntimeError):
                domain_dependency_busy(p, 'ansible')

    def test_quota_interrupts_current_attempt_and_preserves_pending_tasks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'task_logs').mkdir()
            (root / 'manifest.json').write_text(json.dumps(dict(
                hashes={}, credential_env=str(root / '.env'), domains={'repo': ['bad', 'next']},
                total=2, python='python', mini_python='python', task_process_timeout_seconds=3600,
                pause_on_api_budget_exhaustion=True)))
            launched = []

            class Process:
                pid = 999999999
                returncode = None
                def __init__(self, command, **kwargs):
                    launched.append(command)
                    out = Path(command[command.index('--output-dir') + 1])
                    (out / 'analysis').mkdir(parents=True)
                    (out / 'analysis/worker.log').write_text('Budget has been exceeded!')
                def poll(self):
                    return self.returncode
                def wait(self, **kwargs):
                    self.returncode = -15
                    return self.returncode

            with patch('experiments.run_swebench_mini_batch.subprocess.Popen', Process), \
                 patch('experiments.run_swebench_mini_batch.cleanup', return_value=[]), \
                 patch('experiments.run_swebench_mini_batch.os.killpg'), \
                 patch('experiments.run_swebench_mini_batch.signal.signal'):
                run(root)
            state = json.loads((root / 'status.json').read_text())
            self.assertEqual(len(launched), 1)
            self.assertEqual(state['status'], 'interrupted')
            self.assertEqual(state['pause_reason']['reason'], 'api_budget_exhausted')
            self.assertEqual(state['tasks']['bad']['status'], 'interrupted')
            self.assertEqual(state['tasks']['next']['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
