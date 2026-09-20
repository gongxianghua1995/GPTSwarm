"""
SWE-bench Code IO Agent.

A simple input-output agent for SWE-bench tasks that directly generates patches.
"""

from typing import Dict, Any, Optional
from swarm.graph import Graph
from swarm.environment.operations.direct_answer import DirectAnswer
from swarm.environment.agents.agent_registry import AgentRegistry


@AgentRegistry.register('SWECodeIO')
class SWECodeIOAgent(Graph):
    """
    Simple IO agent for SWE-bench tasks.

    Takes the problem description and directly outputs a patch.
    This serves as a baseline agent for SWE-bench evaluation.
    """

    def __init__(
        self,
        domain: str = 'swe_bench',
        model_name: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(domain=domain, model_name=model_name, **kwargs)

    def build_graph(self):
        """Build the agent graph - simple single-node direct answer."""
        operation = DirectAnswer(self.domain, self.model_name)
        self.add_node(operation)
        self.input_nodes = [operation]
        self.output_nodes = [operation]
