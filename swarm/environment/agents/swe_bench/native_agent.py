"""The same tool-capable agent for single-agent and native Swarm baselines."""
import random
import re
import shlex
from collections import Counter

from swarm.graph import Graph, Node
from swarm.environment.agents.agent_registry import AgentRegistry
from swarm.environment.operations.operation_registry import OperationRegistry
from swarm.environment.operations.swe_bench.code_edit import SearchReplaceEdit, _is_test_path
from swarm.environment.operations.swe_bench.test_feedback import TestFeedbackLoop, _bash, REPRO_TIMEOUT
from swarm.environment.domain.swe_bench.env import exec_in_container


class WorkspaceInput(Node):
    def __init__(self):
        super().__init__('Bind the private agent workspace', None, True)
        self.slot = 0

    async def _execute(self, inputs, **kwargs):
        value = dict(self.process_input(inputs)[0])
        value['container'] = value.pop('containers')[self.slot]
        value['agent_index'] = self.slot
        return value


class SourceEdit(SearchReplaceEdit):
    """Existing real-file editor, with issue-derived source search evidence."""
    async def _locate_files(self, task, container, listing):
        terms = list(dict.fromkeys(re.findall(r'\b[A-Za-z_]\w{4,}\b', task)))[:12]
        evidence = ''
        if terms:
            command = ['git', '-C', '/testbed', 'grep', '-n', '-F']
            for term in terms:
                command.extend(['-e', term])
            command.extend(['--', '*.py', ':!**/tests/**', ':!**/test_*.py'])
            result = exec_in_container(container, command, timeout=60)
            evidence = result.stdout[:6000]
        hits = list(dict.fromkeys(line.split(':', 1)[0] for line in evidence.splitlines()))
        ranked = [p for p in hits if p in listing]
        ranked.extend(p for p in listing if p not in ranked)
        self.search_evidence = evidence
        return await super()._locate_files(
            task + '\n\n## Source search evidence\n' + evidence, container, ranked)

    def _syntax_check(self, container, files):
        reverted = []
        for path in files:
            if path.endswith('.py'):
                result = exec_in_container(container, ['bash', '-c',
                    'source /opt/miniconda3/bin/activate testbed && cd /testbed && '
                    'python -m py_compile ' + shlex.quote(path)], timeout=60)
                if result.returncode:
                    exec_in_container(container, ['git', '-C', '/testbed',
                                      'restore', '--', path], timeout=60)
                    reverted.append(path)
        return reverted

    async def _execute(self, inputs, **kwargs):
        results = await super()._execute(inputs, **kwargs)
        for result in results:
            result['search_evidence'] = getattr(self, 'search_evidence', '')
        return results


class LocalVerification(TestFeedbackLoop):
    """Bounded local feedback; timeout is recorded rather than losing the patch."""
    def _run_repro(self, container, script):
        try:
            if not self._write_file(container, '/tmp/repro.py', script):
                raise RuntimeError('Could not write reproduction script')
            result = _bash(container, 'python /tmp/repro.py 2>&1', REPRO_TIMEOUT)
            output = (result.stdout or '')[-6000:]
            passed = result.returncode == 0 and 'REPRO_PASS' in output.splitlines()
            self.checks.append({'kind': 'reproduction', 'script': script,
                                'exit_code': result.returncode, 'passed': passed,
                                'output': output})
            # The inherited loop searches for the marker as a substring.
            # Only expose that marker on a successful process and exact line.
            return output if passed else output.replace('REPRO_PASS', 'REPRO_UNCONFIRMED')
        except Exception as exc:
            message = f'REPRO_ERROR: {type(exc).__name__}: {exc}'
            self.checks.append({'kind': 'reproduction', 'passed': False, 'output': message})
            return message

    def _run_regressions(self, container, repo, pass_to_pass):
        output = super()._run_regressions(container, repo, pass_to_pass)
        self.checks.append({'kind': 'regression', 'output': output,
                            'status': 'unavailable' if not output else
                            ('passed' if output.startswith('exit=0') else 'failed')})
        return output

    async def _execute(self, inputs, **kwargs):
        self.checks = []
        results = await super()._execute(inputs, **kwargs)
        for result in results:
            result['checks'] = self.checks
        return results


class WorkspaceDiff(Node):
    """One candidate per agent, exclusively from its actual workspace."""
    def __init__(self, workspace):
        super().__init__('Export and validate workspace diff', None, True)
        self.workspace = workspace

    async def _execute(self, inputs, **kwargs):
        source = self.workspace.outputs[0]
        container = source['container']
        result = exec_in_container(container, ['git', '-C', '/testbed', 'diff', 'HEAD'], timeout=60)
        patch = result.stdout
        valid = result.returncode == 0 and bool(patch.strip())
        errors = []
        if result.returncode:
            errors.append(result.stderr)
        names = exec_in_container(container, ['git', '-C', '/testbed', 'diff',
                                             '--name-only', 'HEAD'], timeout=60)
        if names.returncode:
            valid = False
            errors.append(names.stderr)
        files = names.stdout.splitlines()
        if any(_is_test_path(p) for p in files):
            valid = False
            errors.append('Candidate modifies test files')
        for path in files:
            if path.endswith('.py'):
                check = exec_in_container(container, ['bash', '-c',
                    'source /opt/miniconda3/bin/activate testbed && cd /testbed && '
                    'python -m py_compile ' + shlex.quote(path)], timeout=60)
                if check.returncode:
                    valid = False
                    errors.append(check.stderr or check.stdout)
        if patch.strip():
            check = exec_in_container(container, ['git', '-C', '/testbed', 'apply',
                '--reverse', '--check', '-'], timeout=60, input_text=patch)
            if check.returncode:
                valid = False
                errors.append(check.stderr)
        return {'operation': self.node_name, 'agent_index': source['agent_index'],
                'task': source['task'], 'container': container, 'raw_patch': patch,
                'output': patch if valid else '', 'valid': valid,
                'validation_errors': errors, 'changed_files': files, 'format': 'patch'}


@AgentRegistry.register('SWECapableAgent')
class SWECapableAgent(Graph):
    def build_graph(self):
        workspace = WorkspaceInput()
        edit = SourceEdit(self.domain, self.model_name)
        verify = LocalVerification(self.domain, self.model_name)
        diff = WorkspaceDiff(workspace)
        for node in [workspace, edit, verify, diff]:
            self.add_node(node)
        workspace.add_successor(edit)
        edit.add_successor(verify)
        verify.add_successor(diff)
        self.input_nodes = [workspace]
        self.output_nodes = [diff]


@OperationRegistry.register('NativePatchVote')
class NativePatchVote(Node):
    """One vote per agent; select an existing valid patch without rewriting it."""
    def __init__(self, domain, model_name, seed=0, **kwargs):
        super().__init__('Vote on independent patches', None, True)
        self.seed = seed

    async def _execute(self, inputs, **kwargs):
        candidates = self.process_input(inputs)
        ids = [p['agent_index'] for p in candidates]
        if len(set(ids)) != len(ids):
            raise ValueError('Each agent must contribute exactly one candidate')
        usable = [p for p in candidates if p.get('valid') and p.get('output', '').strip()]
        counts = Counter(p['output'] for p in usable)
        selected = ''
        tied = []
        if counts:
            tied = [patch for patch, n in counts.items() if n == max(counts.values())]
            selected = random.Random(self.seed).choice(tied)
        return {'operation': self.node_name, 'output': selected, 'format': 'patch',
                'candidate_count': len(candidates), 'valid_candidates': len(usable),
                'distinct_patches': len(counts), 'tie_count': len(tied),
                'selected_agents': [p['agent_index'] for p in usable if p['output'] == selected],
                'votes': [{'agent_index': p['agent_index'], 'valid': p['valid'],
                           'patch_chars': len(p['output'])} for p in candidates]}
