"""Standalone installed mini-swe-agent worker; no GPTSwarm imports required.

The parent passes credentials via environment only. The installed upstream
DefaultAgent owns reasoning/action iteration; this file adapts Docker and logs.
"""
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP', 'True')

import minisweagent
import yaml
from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.litellm_model import LitellmModel
from minisweagent.exceptions import TimeExceeded
from mini_checks import CHECK_SCRIPT, check_evidence
from mini_prompts import ROLE_SYSTEM
from mini_protocol import parse_handoff, assess_review
from mini_policy import REPORT_GUIDANCE, request_budget, inherited_review_criteria


def main(request_path):
    req = json.loads(Path(request_path).read_text())
    out = Path(req['output_dir'])
    started = time.monotonic()
    deadline = started + req['seconds']
    soft_deadline = started + req['soft_seconds']

    def record(event, **values):
        with (out / 'events.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(event=event, elapsed=time.monotonic()-started,
                                         **values), ensure_ascii=False) + '\n')

    class DockerEnvironment(LocalEnvironment):
        journal_offsets = {}
        checkpoint_hash = None
        disclosed = req['phase'] != 'review'
        peers_requested = False
        check_number = 0
        collected_checks = []
        initial_criteria = (inherited_review_criteria(req['incoming_messages'])
                            if req['phase'] == 'final_review' else [])
        latest_checkpoint = None

        def shell(self, command, container=None):
            seconds = max(1, min(req['command_timeout'], int(deadline-time.monotonic())))
            argv = ['docker', 'exec', '-w', req['cwd'],
                    *[part for value in req.get('shell_env', ['BASH_ENV=/root/.bashrc']) for part in ['-e', value]],
                    container or req['container'], 'timeout', '-k', '2', str(seconds),
                    'bash', '-o', 'pipefail', '-c', command]
            try:
                p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors='replace', timeout=seconds+5)
                return dict(output=p.stdout, returncode=p.returncode, exception_info='')
            except subprocess.TimeoutExpired as exc:
                raw = exc.output or b''
                return dict(output=raw.decode(errors='replace') if isinstance(raw, bytes) else raw,
                            returncode=124, exception_info='Command timed out')

        def tree(self, container=None):
            result = self.shell('(git diff HEAD --binary; git ls-files --others --exclude-standard -z '
                                '| xargs -0 -r git hash-object --) | sha256sum', container)
            return result['output'].strip() if result['returncode'] == 0 else None

        def checkpoint(self):
            raw = self.shell('cat /tmp/swarm-handoff.json 2>/dev/null')['output']
            value = parse_handoff(raw)
            if value is None:
                return '', False
            self.latest_checkpoint = value
            key = hashlib.sha256(raw.encode()).hexdigest()
            if key != self.checkpoint_hash:
                self.checkpoint_hash = key
                (out / 'checkpoint.json').write_text(json.dumps(value, ensure_ascii=False, indent=2))
                record('checkpoint', report=value)
            if req['phase'] == 'final_review' and not self.initial_criteria:
                criteria = value.get('acceptance_criteria', [])
                if criteria and all(isinstance(c, str) and c.strip() for c in criteria):
                    self.initial_criteria = list(criteria)
                    record('acceptance_established', criteria=criteria, independent=False)
            if req['role'] == 'Reviewer' and not self.disclosed:
                criteria = value.get('acceptance_criteria', [])
                if criteria and all(isinstance(c, str) and c.strip() for c in criteria):
                    self.disclosed = True
                    self.initial_criteria = criteria
                    record('acceptance_precommitted', criteria=criteria, assumptions=value.get('assumptions', []))
                    record('predecessors_disclosed', message_ids=[m['id'] for m in req['incoming_messages']])
                    return ('\nYour initial checklist is recorded. Predecessor messages now delivered:\n' +
                            json.dumps(req['incoming_messages'], ensure_ascii=False)), True
            return '', True

        def execute(self, action, **kwargs):
            command = action.get('command', '')
            baseline = command.strip().startswith('swe-check --baseline ')
            container = req.get('baseline_container') if baseline else req['container']
            if baseline and not container:
                return dict(output='Baseline comparison is available to Reviewer only.', returncode=2, exception_info='')
            actual = command.strip().replace('swe-check --baseline ', 'swe-check ', 1) if baseline else command
            before = self.tree(container)
            record('action_started', command=command)
            result = self.shell(actual, container)
            after = self.tree(container)
            record('action', command=command, result=result, check=check_evidence(command, result))
            journal = self.shell('cat /tmp/swarm-checks.jsonl 2>/dev/null', container)['output'].splitlines()
            new = journal[self.journal_offsets.get(container, 0):]
            self.journal_offsets[container] = len(journal)
            evidence = []
            for line in new:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                self.check_number += 1
                checked_command = shlex.join(entry['argv'])
                classified = check_evidence('swe-check ' + checked_command, entry)
                item = dict(id=f'{req["phase"]}-C{self.check_number:03d}', command=checked_command,
                            scope='baseline' if baseline else 'patched', wrapped=True,
                            tree_before=before, tree_after=after, **classified)
                record('check', **item)
                evidence.append(item)
            raw_check = check_evidence(command, result)
            if not new and raw_check:
                self.check_number += 1
                item = dict(id=f'{req["phase"]}-R{self.check_number:03d}', command=command,
                            scope='baseline' if baseline else 'patched', wrapped=False,
                            tree_before=before, tree_after=after, **raw_check)
                record('check', **item)
                evidence.append(item)
            self.collected_checks.extend(evidence)
            disclosure, has_checkpoint = self.checkpoint()
            approval_feedback = ''
            if req['role'] == 'Reviewer' and self.latest_checkpoint and self.latest_checkpoint.get('verdict') == 'APPROVE':
                _, errors = assess_review(self.latest_checkpoint, 'Submitted', self.collected_checks,
                                           self.tree(), self.initial_criteria)
                if errors:
                    approval_feedback = '\nApproval evidence is incomplete: ' + json.dumps(errors)
            # If the first checklist was written together with a submit command,
            # return disclosure as an observation so a subsequent request sees it.
            if req['phase'] == 'review' and not self.peers_requested:
                result = {**result, 'output': result['output'].replace(
                    'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT', '[submission deferred]')}
                disclosure += '\nRead the predecessor messages before submitting your review.'
            else:
                self._check_finished(result)
            remaining = max(0, int(deadline-time.monotonic()))
            soft_remaining = int(soft_deadline-time.monotonic())
            guidance = (f'\n[Shared task: {remaining} seconds remain for all remaining roles. '
                        f'Role handoff target in {soft_remaining} seconds; this is advisory.]')
            if not has_checkpoint:
                guidance += ' Save an initial valid /tmp/swarm-handoff.json now, even if incomplete.'
            if min(remaining, soft_remaining) <= req['report_seconds']:
                guidance += ' ' + REPORT_GUIDANCE
            elif req['role'] == 'Engineer' and time.monotonic()-started > req['soft_seconds'] * .25:
                guidance += ' If source context is sufficient, implement a focused change now and verify it.'
            return {**result, 'output': result['output'] + '\nRecorded check evidence:\n' +
                    json.dumps(evidence) + disclosure + approval_feedback + guidance}

    class ObservedModel(LitellmModel):
        # A deliberate local budget exit is not a transient provider failure.
        abort_exceptions = [*LitellmModel.abort_exceptions, TimeExceeded]

        def _query(self, messages, **kwargs):
            remaining = max(0, deadline-time.monotonic())
            budget = request_budget(remaining, req['api_timeout'], req['report_seconds'],
                                    soft_remaining=soft_deadline-time.monotonic())
            if not budget['can_request']:
                record('model_request_skipped', reason='Shared task deadline reached', remaining=remaining)
                raise TimeExceeded(dict(role='exit', content='Task time exhausted; recover checkpoint.',
                                        extra=dict(exit_status='TaskBudgetExhausted', submission='')))
            reporting = budget['reporting']
            if reporting:
                messages = messages + [dict(role='user', content=REPORT_GUIDANCE +
                    f' Shared task time left: {int(remaining)} seconds. The role handoff target is '
                    'advisory; an active request is not cut off at that target. Leave time for '
                    'downstream review and repair by submitting your findings promptly.')]
            if agent.n_calls > 1 and env.latest_checkpoint is None:
                messages = messages + [dict(role='user', content='Before further exploration, your next action '
                    'must save a concise valid /tmp/swarm-handoff.json. Record unknowns as open questions; '
                    'you can refine the report after focused checks.')]
            request_timeout = budget['timeout']
            env.peers_requested = env.disclosed
            record('model_request', messages=messages, max_tokens=req['max_tokens'],
                   timeout=request_timeout, reporting=reporting,
                   predecessors_in_context=env.peers_requested)
            try:
                response = super()._query(messages, **(kwargs | {
                    'timeout': request_timeout}))
            except Exception as exc:
                record('model_error', error=type(exc).__name__)
                raise
            record('model_response', usage=response.usage.model_dump() if response.usage else {},
                   finish_reason=response.choices[0].finish_reason,
                   message=response.choices[0].message.model_dump())
            return response

    root = Path(minisweagent.__file__).parent
    config_path = root / 'config/benchmarks/swebench.yaml'
    cfg = yaml.safe_load(config_path.read_text())
    cfg['model'].update(model_name='openai/' + req['model'], cost_tracking='ignore_errors',
                        model_kwargs=dict(temperature=req['temperature'], max_tokens=req['max_tokens'],
                                          timeout=req['api_timeout'], num_retries=0))
    cfg['agent'].update(step_limit=req['steps'], cost_limit=0,
                        wall_time_limit_seconds=max(1, int(req['seconds'])),
                        output_path=out / 'trajectory.json')
    cfg['agent']['system_template'] += '\n\n' + ROLE_SYSTEM[req['role']]
    # Keep upstream tool protocol and DefaultAgent, specialize the assignment.
    # Every role submits a message; the host separately exports the real diff.
    cfg['agent']['instance_template'] = '''{{task}}

Use bash tools to inspect the actual repository. Each action runs in a fresh
shell in the configured repository. The container has no external network.
Use local dependencies; never fetch a solution or inspect evaluator artifacts.
Do not commit, reset the repository, alter .git, or modify tracked tests.
Keep temporary scripts and reports in /tmp. Use swe-check followed by the
actual test command, without pipelines, to preserve exit status. Zero tests
or an unavailable test runner is not a passing verification.
The shared task has {{wall_time_limit_seconds}} seconds left for this and all
remaining roles. Each role's handoff target is advisory, not a separate timeout.
Maintain the structured JSON handoff specified above. Submit it with a separate call:
echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat /tmp/swarm-handoff.json
Submit a handoff report, not a patch. The host preserves actual source edits.
'''
    env = DockerEnvironment(cwd=req['cwd'])
    if req['phase'] == 'final_review':
        record('acceptance_inherited', criteria=env.initial_criteria)
        record('predecessors_disclosed', delivery='initial_prompt',
               message_ids=[m['id'] for m in req['incoming_messages']])
    for container in set(filter(None, [req['container'], req.get('baseline_container')])):
        helper = env.shell('rm -f /tmp/swarm-handoff.json /tmp/swarm-checks.jsonl && printf %s ' +
                           shlex.quote(CHECK_SCRIPT) + ' > /usr/local/bin/swe-check && chmod +x /usr/local/bin/swe-check', container)
        if helper['returncode']:
            raise RuntimeError('Cannot install local test evidence helper')
    agent = DefaultAgent(ObservedModel(**cfg['model']), env, **cfg['agent'])
    files = ['agents/default.py', 'models/litellm_model.py', 'config/benchmarks/swebench.yaml']
    record('worker_started', version=minisweagent.__version__,
           agent_class='minisweagent.agents.default.DefaultAgent',
           dependency_hashes={p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in files})
    try:
        result = agent.run(req['task'])
    except Exception as exc:
        result = {'exit_status': type(exc).__name__, 'submission': ''}
    result.update(n_calls=agent.n_calls, elapsed_seconds=time.monotonic()-started, final_tree=env.tree())
    (out / 'result.json').write_text(json.dumps(result, ensure_ascii=False))
    record('worker_finished', exit_status=result.get('exit_status'), n_calls=agent.n_calls)


if __name__ == '__main__':
    main(sys.argv[1])
