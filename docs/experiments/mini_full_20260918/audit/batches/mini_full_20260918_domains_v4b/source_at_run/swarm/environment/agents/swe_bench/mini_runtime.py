"""Docker lifecycle and isolated process adapter for fixed-role Swarm nodes."""
import asyncio
import hashlib
import json
import os
import re
import shlex
import sys
import time
import uuid
from pathlib import Path
from .mini_prompts import role_task, handoff_messages
from .mini_protocol import parse_handoff, assess_review


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def permission_only_diff(raw):
    """Accept only regular-file executable-bit changes with identical blobs.

    Input is git diff --raw --no-abbrev -z, without rename detection.
    Symlinks, content changes, additions, removals and malformed records fail.
    """
    records = raw.split('\0')
    if not raw or records[-1] != '' or len(records) % 2 != 1:
        return False
    for index in range(0, len(records)-1, 2):
        fields = records[index].split()
        if (len(fields) != 5 or not records[index+1] or fields[4] != 'M'
                or fields[0] not in {':100644', ':100755'}
                or fields[1] not in {'100644', '100755'}
                or fields[0][1:] == fields[1] or fields[2] != fields[3]):
            return False
    return True


def is_test_path(path):
    parts = Path(path).parts
    return any(p in {'test', 'tests', '__tests__'} for p in parts) or Path(path).name.startswith('test_')


async def command(argv, *, seconds=120, input_text=None, check=True):
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input_text.encode() if input_text is not None else None), seconds)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    result = {'returncode': proc.returncode, 'output': stdout.decode(errors='replace'),
              'stderr': stderr.decode(errors='replace')}
    if check and proc.returncode:
        raise RuntimeError(f'Command {argv[0]} exited {proc.returncode}: {result["stderr"][-1500:]}')
    return result


def merge_messages(inputs):
    messages = {}
    for value in inputs:
        for message in value.get('messages', []):
            messages[message['id']] = message
    return list(messages.values())


def review_verdict(report, exit_status):
    if exit_status != 'Submitted':
        return 'unverified'
    # A whole-line protocol; prose such as "not APPROVE" cannot approve a patch.
    matches = re.findall(r'^VERDICT: (APPROVE|REQUEST_CHANGES|UNVERIFIED)\s*$', report, re.M)
    return matches[0].lower() if len(matches) == 1 else 'unverified'


def interrupted_handoff(role, status, observations, partial_report=''):
    """Preserve concrete work even when a role cannot formally submit in time."""
    report = f'{role} exited with {status}; this is an incomplete handoff, not approval.\n'
    if partial_report.strip():
        report += '\nPartial report recovered from workspace:\n' + partial_report[:16000]
    report += '\nRecent tool observations (may include unsuccessful checks):\n'
    report += json.dumps(observations[-8:], ensure_ascii=False)
    return report


class MiniRuntime:
    def __init__(self, record, config, out, mini_python=sys.executable):
        self.record = record
        self.config = config
        self.out = Path(out).resolve()
        self.mini_python = mini_python
        self.started = time.monotonic()
        self.deadline = self.started + config['task_seconds']
        self.containers = set()
        self.initial_untracked = {}
        self.main = None
        self.base = record['base_commit']
        self.cwd = '/testbed'

    def event(self, event, **values):
        with (self.out / 'runtime.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(event=event, elapsed=time.monotonic()-self.started,
                                         **values), ensure_ascii=False) + '\n')

    async def shell(self, container, script, *, seconds=120, input_text=None, check=True):
        argv = ['docker', 'exec', '-i', '-w', self.cwd, '-e', 'BASH_ENV=/root/.bashrc',
                container, 'timeout', '-k', '2', str(seconds), 'bash', '-o', 'pipefail', '-c', script]
        return await command(argv, seconds=seconds+5, input_text=input_text, check=check)

    async def start_workspace(self, purpose, patch=''):
        name = 'gptswarm_mini_' + purpose + '_' + uuid.uuid4().hex[:12]
        image = 'swebench/sweb.eval.x86_64.' + self.record['instance_id'].replace('__', '_1776_') + ':latest'
        self.containers.add(name)
        await command(['docker', 'run', '-d', '--name', name, '--network', 'none',
                       '--label', 'gptswarm.run=' + self.out.name,
                       '-w', self.cwd, '--entrypoint', '/bin/bash', image,
                       '-c', 'exec tail -f /dev/null'])
        inspection = json.loads((await command(['docker', 'inspect', name]))['output'])[0]
        if inspection['HostConfig']['NetworkMode'] != 'none':
            raise RuntimeError('Generation container is not network isolated')
        head = (await self.shell(name, 'git rev-parse HEAD'))['output'].strip()
        if head != self.base:
            # Image builders may commit setup edits (e.g. Sphinx's tox.ini) or
            # chmod changes. Restore the exact dataset base BEFORE any agent
            # can inspect code; the image HEAD is not the generation baseline.
            trees = (await self.shell(name, 'git rev-parse HEAD^{tree} ' +
                                     shlex.quote(self.base + '^{tree}')))['output'].splitlines()
            if len(trees) != 2:
                raise RuntimeError('Image source tree differs from task base commit')
            if trees[0] != trees[1]:
                raw = (await self.shell(name, 'git diff --raw --no-abbrev --no-renames -z ' +
                                        shlex.quote(self.base) + ' HEAD'))['output']
                self.event('image_permission_difference' if permission_only_diff(raw)
                           else 'image_baseline_difference', container=name, image=image,
                           files=len(raw.split('\0'))//2,
                           policy='restore exact dataset base before agent access')
        dirty = (await self.shell(name, 'git diff HEAD --name-only'))['output']
        if dirty.strip():
            raise RuntimeError('Image contains tracked modifications before generation')
        await self.shell(name, 'git checkout --detach ' + shlex.quote(self.base))
        restored = (await self.shell(name, 'git rev-parse HEAD'))['output'].strip()
        if restored != self.base:
            raise RuntimeError('Failed to restore exact task base commit')
        await self.shell(name, 'git diff --exit-code HEAD -- && git diff --cached --exit-code HEAD --')
        # Clone only the current commit over the local transport. Remove the
        # original object store, refs and reflogs from this disposable container.
        script = '''set -eu
swarm_git_tmp=$(mktemp -d /tmp/swarm-git.XXXXXXXX)
git clone --quiet --no-local --depth 1 --no-checkout "file://$PWD" "$swarm_git_tmp"
rm -rf .git
mv "$swarm_git_tmp/.git" .git
rmdir "$swarm_git_tmp"
git reset --mixed HEAD >/dev/null
git remote remove origin
test "$(git rev-list --all --count)" = 1
'''
        await self.shell(name, script)
        shallow = (await self.shell(name, 'git rev-parse --is-shallow-repository'))['output'].strip()
        if shallow != 'true':
            raise RuntimeError('Expected a base-only shallow repository')
        initial = (await self.shell(name, 'git ls-files --others --exclude-standard -z'))['output']
        self.initial_untracked[name] = set(filter(None, initial.split('\0')))
        if patch:
            await self.shell(name, 'git apply --binary -', input_text=patch)
        self.event('workspace_started', purpose=purpose, container=name, image=image,
                   network=inspection['HostConfig']['NetworkMode'], image_head=head, base_commit=self.base,
                   shallow=True, patch_sha256=digest(patch))
        return name

    async def prepare(self):
        self.main = await self.start_workspace('engineer')

    async def export(self, container=None, *, include_tests=False):
        container = container or self.main
        if not container:
            return {'patch': '', 'sha256': digest(''), 'excluded_paths': []}
        changed = (await self.shell(container, 'git diff HEAD --name-only -z'))['output']
        untracked = (await self.shell(container, 'git ls-files --others --exclude-standard -z'))['output']
        paths = set(filter(None, changed.split('\0')))
        paths.update(set(filter(None, untracked.split('\0'))) - self.initial_untracked[container])
        excluded = sorted(p for p in paths if not include_tests and is_test_path(p))
        paths -= set(excluded)
        if not paths:
            return {'patch': '', 'sha256': digest(''), 'excluded_paths': excluded}
        # A separate index includes unstaged, staged, added and deleted files
        # without changing the agent's index or relying on model diff text.
        script = '''set -eu
swarm_index=$(mktemp /tmp/swarm-index.XXXXXXXX)
rm "$swarm_index"
export GIT_INDEX_FILE="$swarm_index"
trap 'rm -f "$GIT_INDEX_FILE"' EXIT
git read-tree HEAD
git add -A -- ''' + ' '.join(shlex.quote(':(literal)' + p) for p in sorted(paths)) + '''
git diff --cached --binary HEAD
'''
        patch = (await self.shell(container, script))['output']
        return {'patch': patch, 'sha256': digest(patch), 'excluded_paths': excluded}

    async def stop_workspace(self, name):
        await command(['docker', 'rm', '-f', name], check=False)
        self.containers.discard(name)

    async def close(self):
        for name in list(self.containers):
            await self.stop_workspace(name)

    async def run_worker(self, request, folder, seconds):
        request_path = folder / 'request.json'
        request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2))
        env = os.environ.copy()
        if env.get('OPENAI_BASE_URL') and not env.get('OPENAI_API_BASE'):
            env['OPENAI_API_BASE'] = env['OPENAI_BASE_URL']
        worker = self.out / 'source_at_run/swarm/environment/agents/swe_bench/mini_worker.py'
        with (folder / 'worker.log').open('w') as log:
            proc = await asyncio.create_subprocess_exec(self.mini_python, str(worker), str(request_path),
                                                        env=env, stdout=log, stderr=log)
            try:
                await asyncio.wait_for(proc.wait(), seconds)
            finally:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), 3)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()

    async def phase(self, phase, role, messages):
        limit = self.config['phases'][phase]
        phase_started = time.monotonic()
        # Phase seconds guide handoff timing; all nodes share the task deadline.
        phase_deadline = self.deadline
        folder = self.out / phase
        folder.mkdir()
        source = await self.export()
        snapshot = phase in {'analysis', 'review', 'final_review'}
        container = baseline_container = None
        status, report, checks, usage, observations = 'BudgetExhausted', '', [], [], []
        final_tree, precommitted = None, []
        try:
            if phase_deadline - time.monotonic() < 2:
                return dict(exit_status=status, report=report, checks=checks, usage=usage,
                            patch_sha256=source['sha256'], verdict='unverified')
            container = await self.start_workspace(phase, source['patch']) if snapshot else self.main
            if role == 'Reviewer':
                baseline_container = await self.start_workspace(phase + '_base')
            before = await self.export(container, include_tests=True)
            seconds = phase_deadline - time.monotonic()
            if seconds < 2:
                raise asyncio.TimeoutError()
            task = self.assignment(phase, role, messages)
            request = dict(task=task, role=role, phase=phase, container=container, cwd=self.cwd,
                           baseline_container=baseline_container, incoming_messages=handoff_messages(messages),
                           report_seconds=self.config.get('report_seconds', 60),
                           soft_seconds=max(0, limit['seconds'] - (time.monotonic() - phase_started)),
                           output_dir=str(folder), seconds=seconds, steps=limit['steps'],
                           max_tokens=limit['max_tokens'], model=self.config['model'],
                           temperature=self.config['temperature'], api_timeout=self.config['api_timeout'],
                           command_timeout=self.config['command_timeout'])
            self.event('phase_started', phase=phase, role=role, seconds=seconds,
                       budget_mode='shared_task', soft_seconds=request['soft_seconds'],
                       incoming_message_ids=[m['id'] for m in messages], patch_sha256=source['sha256'])
            try:
                await self.run_worker(request, folder, seconds)
                result_file = folder / 'result.json'
                if result_file.exists():
                    result = json.loads(result_file.read_text())
                    status, report = result.get('exit_status', 'Unknown'), result.get('submission', '')
                    final_tree = result.get('final_tree')
                else:
                    status = 'WorkerError'
            except asyncio.TimeoutError:
                status = 'PhaseTimeout'
                # The container-side timeout has at most two seconds of kill
                # grace. Let it finish before reading/reviewing the workspace.
                await asyncio.sleep(3)
            events = folder / 'events.jsonl'
            if events.exists():
                for line in events.read_text().splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event['event'] == 'check':
                        checks.append({k: v for k, v in event.items() if k not in {'event', 'elapsed'}})
                    if event['event'] in {'acceptance_precommitted', 'acceptance_inherited', 'acceptance_established'}:
                        precommitted = event['criteria']
                    if event['event'] == 'action':
                        observations.append(dict(command=event['command'][:1200],
                                                 returncode=event['result']['returncode'],
                                                 output=event['result']['output'][-2200:]))
                    if event['event'] == 'model_response':
                        usage.append(dict(usage=event['usage'], finish_reason=event['finish_reason']))
            structured = parse_handoff(report)
            if not report:
                checkpoint = folder / 'checkpoint.json'
                if checkpoint.exists():
                    structured = parse_handoff(checkpoint.read_text())
                if structured:
                    structured['open_questions'] = list(structured['open_questions']) + [f'Phase ended with {status}; report is incomplete.']
                    report = json.dumps(structured, ensure_ascii=False)
                else:
                    partial = await self.shell(container, 'cat /tmp/swarm-handoff.json', seconds=5, check=False)
                    report = interrupted_handoff(role, status, observations,
                                                 partial['output'] if partial['returncode'] == 0 else '')
            after = await self.export(container, include_tests=True)
            if snapshot and before['sha256'] != after['sha256']:
                status = 'ReadOnlyViolation'
                report += '\nSource/test files changed in the inspection snapshot. These changes were not propagated.'
            final = await self.export()
            (folder / 'workspace.patch').write_text(final['patch'])
            verdict, approval_errors = (assess_review(structured, status, checks, final_tree, precommitted)
                                        if role == 'Reviewer' else (None, []))
            if approval_errors:
                report += '\nHost approval checks: ' + json.dumps(approval_errors)
            result = dict(exit_status=status, report=report, checks=checks, usage=usage,
                          structured_report=structured, approval_errors=approval_errors,
                          acceptance_precommitted=bool(precommitted) and phase == 'review',
                          acceptance_origin=('inherited_or_established' if phase == 'final_review' else 'independent'),
                          initial_acceptance=precommitted,
                          patch_sha256=final['sha256'], verdict=verdict,
                          inspected_patch_sha256=source['sha256'] if snapshot else None)
            (folder / 'handoff.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
            self.event('phase_finished', phase=phase, exit_status=status, verdict=verdict,
                       patch_sha256=final['sha256'])
            return result
        finally:
            if snapshot and container:
                await self.stop_workspace(container)
            if baseline_container:
                await self.stop_workspace(baseline_container)

    def assignment(self, phase, role, messages):
        return role_task(phase, role, self.cwd, self.record['problem_statement'], messages)
