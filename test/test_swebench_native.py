"""Behavior checks for equal-capability, isolated native Swarm execution."""
import asyncio
from subprocess import CompletedProcess
from unittest.mock import patch

import unittest

from experiments.run_swebench_native_smoke import build_swarm
from swarm.environment.agents.swe_bench.native_agent import LocalVerification, NativePatchVote, WorkspaceDiff, WorkspaceInput


def test_same_agent_in_single_and_team_with_no_cross_edges():
    single = build_swarm('mock', 1)
    team = build_swarm('mock', 3)
    expected = [n.node_name for n in single.used_agents[0].nodes.values()]
    owners = {n.id: i for i, a in enumerate(team.used_agents) for n in a.nodes.values()}
    assert not team.potential_connections
    for i, agent in enumerate(team.used_agents):
        assert [n.node_name for n in agent.nodes.values()] == expected
        assert agent.input_nodes[0].slot == i
        assert len(agent.output_nodes) == 1
        for node in agent.nodes.values():
            for successor in node.successors:
                assert successor is team.composite_graph.decision_method or owners[node.id] == owners[successor.id]
    assert len(team.composite_graph.decision_method.predecessors) == 3


def test_workspace_binding_does_not_share_other_containers():
    source = {'task': 'fix', 'containers': ['private_a', 'private_b']}
    node = WorkspaceInput()
    node.slot = 1
    bound = asyncio.run(node._execute(source))
    assert bound['container'] == 'private_b'
    assert 'containers' not in bound
    assert source['containers'] == ['private_a', 'private_b']


def test_vote_counts_once_and_ignores_invalid_patches():
    node = NativePatchVote('swe_bench', 'mock')
    candidates = [{'agent_index': i, 'output': p, 'valid': valid}
                  for i, (p, valid) in enumerate([('good', True), ('good', True), ('bad', False)])]
    result = asyncio.run(node._execute(candidates))
    assert result['output'] == 'good'
    assert result['selected_agents'] == [0, 1]
    assert result['candidate_count'] == 3
    with unittest.TestCase().assertRaisesRegex(ValueError, 'exactly one'):
        asyncio.run(node._execute(candidates + [candidates[0]]))
    empty = asyncio.run(node._execute([{'agent_index': 0, 'output': '', 'valid': False}]))
    assert empty['output'] == ''
    ties = [{'agent_index': i, 'output': p, 'valid': True} for i, p in enumerate(['a', 'b', 'c'])]
    assert asyncio.run(node._execute(ties)) == asyncio.run(node._execute(ties))


def test_empty_workspace_never_falls_back_to_llm_patch():
    workspace = WorkspaceInput()
    workspace.outputs = [{'container': 'private_a', 'task': 'fix', 'agent_index': 0}]
    node = WorkspaceDiff(workspace)
    with patch('swarm.environment.agents.swe_bench.native_agent.exec_in_container',
               return_value=CompletedProcess([], 0, '', '')):
        result = asyncio.run(node._execute([{'output': 'diff --git fake'}]))
    assert result['output'] == ''
    assert not result['valid']


def test_invalid_syntax_excludes_actual_diff():
    workspace = WorkspaceInput()
    workspace.outputs = [{'container': 'private_b', 'task': 'fix', 'agent_index': 1}]
    node = WorkspaceDiff(workspace)
    calls = []
    def execute(container, command, **kwargs):
        calls.append((container, command))
        if command[0] == 'bash':
            return CompletedProcess(command, 1, '', 'SyntaxError')
        text = 'source.py\n' if '--name-only' in command else 'diff --git actual\n'
        return CompletedProcess(command, 0, text, '')
    with patch('swarm.environment.agents.swe_bench.native_agent.exec_in_container', side_effect=execute):
        result = asyncio.run(node._execute([]))
    assert result['raw_patch'] == 'diff --git actual\n'
    assert not result['valid']
    assert result['output'] == ''
    assert all(container == 'private_b' for container, _ in calls)


def test_reproduction_marker_requires_successful_process():
    node = LocalVerification('swe_bench', 'mock')
    node.checks = []
    with patch.object(node, '_write_file', return_value=True), patch(
            'swarm.environment.agents.swe_bench.native_agent._bash',
            return_value=CompletedProcess([], 1, 'REPRO_PASS\n', '')):
        result = node._run_repro('private_a', 'print("REPRO_PASS")')
    assert 'REPRO_PASS' not in result
    assert node.checks[-1]['passed'] is False
    with patch.object(node, '_write_file', return_value=True), patch(
            'swarm.environment.agents.swe_bench.native_agent._bash',
            return_value=CompletedProcess([], 0, 'REPRO_PASS\n', '')):
        assert node._run_repro('private_a', 'print("REPRO_PASS")') == 'REPRO_PASS\n'
    assert node.checks[-1]['passed'] is True


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(unittest.FunctionTestCase(fn)
        for name, fn in globals().items() if name.startswith('test_') and callable(fn))
