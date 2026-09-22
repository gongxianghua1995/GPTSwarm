"""Opt-in Docker + installed DefaultAgent contract check, with mocked model replies.

Run with GPTSwarm's Python; pass the mini interpreter, optionally followed
by a dataset JSON and instance ID (including Pro tasks).
No real model requests are made. Only disposable gptswarm_mini_* containers
are created; real shell commands verify the helper and handoff protocol.
"""
import asyncio
import json
from pathlib import Path
import shlex
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def child(request_path):
    request = json.loads(Path(request_path).read_text())
    phase = request['phase']
    sys.path.insert(0, str(ROOT / 'swarm/environment/agents/swe_bench'))
    from unittest.mock import patch
    from litellm import ModelResponse
    from minisweagent.models.litellm_model import LitellmModel
    import mini_worker
    value = dict(summary='Contract smoke', facts=[], assumptions=[], acceptance_criteria=['assertions execute'],
                 changes=[], evidence=[], open_questions=[], findings=[], check_dispositions=[], verdict='UNVERIFIED')

    def write_report(value):
        return 'printf %s ' + shlex.quote(json.dumps(value)) + ' > /tmp/swarm-handoff.json'

    # Reproduce a checklist written together with submission: disclosure must
    # reach the next model request instead of terminating the initial review.
    first = write_report(value)
    if phase == 'review':
        first += '; echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat /tmp/swarm-handoff.json'
    commands = [first,
                'swe-check python -c ' + shlex.quote('assert 2 + 2 == 4; print("1 assertions passed")') + ' | tail -1; echo done',
                'swe-check python -c ' + shlex.quote('import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertEqual(2+2,4)\nunittest.main()'),
                'swe-check --baseline python -c ' + shlex.quote('raise SystemExit(7)') + '; echo masked']
    value['evidence'] = [dict(check_id=phase+'-C001', kind='behavior', claim='assertion ran', criterion=0),
                         dict(check_id=phase+'-C002', kind='regression', claim='test ran', criterion=0)]
    value['verdict'] = 'APPROVE'
    commands.extend([write_report(value), 'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat /tmp/swarm-handoff.json'])
    if phase == 'analysis':
        value['verdict'] = 'UNVERIFIED'
        value['evidence'] = []
        commands = [write_report(value), 'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat /tmp/swarm-handoff.json']
    calls = []

    def respond(model, messages, **kwargs):
        calls.append(messages)
        command = commands[len(calls)-1]
        return ModelResponse(model='mock', choices=[dict(finish_reason='tool_calls', message=dict(
            role='assistant', content='Execute the contract check.', tool_calls=[dict(
                id='call_' + str(len(calls)), type='function', function=dict(name='bash', arguments=json.dumps({'command': command})))]))],
            usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))

    with patch.object(LitellmModel, '_query', respond):
        mini_worker.main(request_path)
    if phase == 'analysis':
        assert len(calls) == 2, 'Remaining time below 45s must still permit handoff calls'
    elif phase == 'review':
        assert 'PEER_SENTINEL' not in json.dumps(calls[0])
        assert 'PEER_SENTINEL' in json.dumps(calls[1])
    else:
        assert 'REVISION_SENTINEL' in json.dumps(calls[0])
        assert 'assertions execute' in json.dumps(calls[0])


async def parent(mini_python, data_path=None, instance_id='django__django-10999'):
    sys.path.insert(0, str(ROOT))
    from swarm.environment.agents.swe_bench.mini_runtime import MiniRuntime
    records = json.loads(Path(data_path or ROOT / 'outputs/swebench/swebench_verified_test_154.json').read_text())
    record = next(r for r in records if r['instance_id'] == instance_id)
    config = json.loads((ROOT / 'config/swebench/mini_fixed.json').read_text())
    config['phases']['review']['seconds'] = 1  # advisory: actual calls must survive this target
    out = Path(tempfile.mkdtemp(prefix='swarm-mini-contract-'))
    runtime = MiniRuntime(record, config, out, mini_python)

    async def run_worker(request, folder, seconds):
        request_path = folder / 'request.json'
        request_path.write_text(json.dumps(request))
        with (folder / 'worker.log').open('w') as log:
            proc = await asyncio.create_subprocess_exec(mini_python, str(Path(__file__).resolve()),
                                                        '--worker-json', str(request_path), stdout=log, stderr=log)
            try:
                await asyncio.wait_for(proc.wait(), seconds)
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            if proc.returncode:
                raise RuntimeError(f'Contract worker failed; see {folder / "worker.log"}')

    runtime.run_worker = run_worker
    try:
        await runtime.prepare()
        result = await runtime.phase('review', 'Reviewer', [dict(id='engineer', phase='implementation', report='PEER_SENTINEL')])
        assert result['exit_status'] == 'Submitted', result
        assert result['verdict'] == 'approve', result['approval_errors']
        assert result['acceptance_precommitted']
        request = json.loads((out/'review/request.json').read_text())
        assert request['soft_seconds'] <= 1 and request['seconds'] > 900
        baseline = next(c for c in result['checks'] if c['scope'] == 'baseline')
        assert baseline['returncode'] == 7
        final = await runtime.phase('final_review', 'Reviewer', [
            dict(id='review', role='Reviewer', phase='review', **result),
            dict(id='revision', role='Engineer', phase='revision', report='REVISION_SENTINEL')])
        assert final['exit_status'] == 'Submitted', final
        assert final['verdict'] == 'approve', final['approval_errors']
        assert final['initial_acceptance'] == ['assertions execute']
        assert not final['acceptance_precommitted']
        runtime.deadline = time.monotonic() + 40
        short = await runtime.phase('analysis', 'Analyst', [])
        assert short['exit_status'] == 'Submitted', short
        print(json.dumps(dict(status='passed', evidence=str(out), checks=len(result['checks']),
                              final_review=final['verdict'], short_budget=short['exit_status'],
                              actual_agent='minisweagent.agents.default.DefaultAgent', model='mocked')))
    finally:
        await runtime.close()


if __name__ == '__main__':
    if sys.argv[1] == '--worker-json':
        child(sys.argv[2])
    else:
        asyncio.run(parent(*sys.argv[1:]))
