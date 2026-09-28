"""Three roles in a fixed, explicitly connected native GPTSwarm DAG."""
from swarm.graph import Graph, Node
from swarm.environment.agents.agent_registry import AgentRegistry
from swarm.environment.operations.operation_registry import OperationRegistry
from .mini_runtime import merge_messages


class MiniRoleNode(Node):
    def __init__(self, role, phase):
        super().__init__(f'{role}: {phase}', None, True)
        self.role, self.phase = role, phase
        self.runtime = None

    @property
    def node_name(self):
        return f'{self.role}.{self.phase}'

    async def _execute(self, inputs, **kwargs):
        messages = merge_messages(self.process_input(inputs))
        review = next((m for m in messages if m['phase'] == 'review'), None)
        current = await self.runtime.export()
        approved = bool(review and review.get('verdict') == 'approve' and
                        review['patch_sha256'] == current['sha256'])
        if approved and self.phase in {'revision', 'final_review'}:
            result = dict(exit_status='SkippedApproved', report='Existing reviewed patch is unchanged.',
                          checks=[], usage=[], patch_sha256=current['sha256'],
                          verdict='approve' if self.phase == 'final_review' else None)
        else:
            try:
                result = await self.runtime.phase(self.phase, self.role, messages)
            except Exception as exc:
                # Preserve the graph's handoff and real workspace on role failure.
                self.runtime.event('phase_error', phase=self.phase, error=type(exc).__name__)
                current = await self.runtime.export()
                result = dict(exit_status='PhaseError', report=f'{self.phase} failed: {type(exc).__name__}',
                              checks=[], usage=[], patch_sha256=current['sha256'], verdict='unverified')
        message = dict(id=self.id, role=self.role, phase=self.phase,
                       **{k: v for k, v in result.items() if k != 'usage'})
        self.runtime.event('message_sent', **message)
        return dict(operation=self.node_name, messages=messages + [message],
                    output=result['report'], usage=result.get('usage', []), format='role_report')


@AgentRegistry.register('SWEMiniAnalyst')
class SWEMiniAnalyst(Graph):
    def build_graph(self):
        node = MiniRoleNode('Analyst', 'analysis')
        self.add_node(node)
        self.input_nodes = self.output_nodes = [node]


@AgentRegistry.register('SWEMiniEngineer')
class SWEMiniEngineer(Graph):
    def build_graph(self):
        initial = MiniRoleNode('Engineer', 'implementation')
        revision = MiniRoleNode('Engineer', 'revision')
        for node in [initial, revision]:
            self.add_node(node)
        initial.add_successor(revision)
        self.input_nodes, self.output_nodes = [initial], [revision]


@AgentRegistry.register('SWEMiniReviewer')
class SWEMiniReviewer(Graph):
    def build_graph(self):
        initial = MiniRoleNode('Reviewer', 'review')
        final = MiniRoleNode('Reviewer', 'final_review')
        for node in [initial, final]:
            self.add_node(node)
        initial.add_successor(final)
        self.input_nodes, self.output_nodes = [initial], [final]


@OperationRegistry.register('MiniWorkspaceSubmission')
class MiniWorkspaceSubmission(Node):
    def __init__(self, domain, model_name, **kwargs):
        super().__init__('Export the collaboratively edited workspace', None, True)
        self.runtime = None

    async def _execute(self, inputs, **kwargs):
        messages = merge_messages(self.process_input(inputs))
        patch = await self.runtime.export()
        latest = next((m for m in reversed(messages) if m['role'] == 'Reviewer'), {})
        verdict = latest.get('verdict', 'unverified')
        if latest.get('patch_sha256') != patch['sha256']:
            verdict = 'unverified'
        return dict(operation=self.node_name, output=patch['patch'], format='patch',
                    patch_sha256=patch['sha256'], excluded_paths=patch['excluded_paths'],
                    local_verdict=verdict, messages=messages)


def build_fixed_swarm(model_name, runtime):
    from swarm.graph.swarm import Swarm
    swarm = Swarm(['SWEMiniAnalyst', 'SWEMiniEngineer', 'SWEMiniReviewer'],
                  'swe_bench', model_name=model_name, edge_optimize=False, node_optimize=False,
                  final_node_class='MiniWorkspaceSubmission', final_node_kwargs={})
    graph = swarm.composite_graph
    final = graph.decision_method
    # Disabling optimization alone creates no communication edges. Explicitly
    # wire the fixed baseline instead of relying on that independent default.
    for node in list(final.predecessors):
        node.remove_successor(final)
    analyst = swarm.used_agents[0].input_nodes[0]
    engineer, revision = list(swarm.used_agents[1].nodes.values())
    reviewer, recheck = list(swarm.used_agents[2].nodes.values())
    analyst.add_successor(engineer)
    engineer.add_successor(reviewer)
    reviewer.add_successor(revision)
    revision.add_successor(recheck)
    recheck.add_successor(final)
    # Only the root gets task input. Otherwise Node.execute prioritizes direct
    # inputs and silently bypasses messages delivered by predecessor nodes.
    graph.input_nodes = [analyst]
    for node in graph.nodes.values():
        node.runtime = runtime
    assert not swarm.potential_connections
    return swarm
