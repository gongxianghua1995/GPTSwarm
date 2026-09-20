"""
SWE-bench ReAct Agent.

An agent that uses ReAct (Reasoning + Acting) style reasoning for SWE-bench tasks.
"""

from typing import Dict, Any, Optional, List
from swarm.graph import Graph
from swarm.environment.operations.cot_step import CoTStep
from swarm.environment.operations.final_decision import FinalDecision
from swarm.environment.agents.agent_registry import AgentRegistry


@AgentRegistry.register('SWEReAct')
class SWEReActAgent(Graph):
    """
    ReAct-style agent for SWE-bench tasks.

    Uses chain-of-thought reasoning with multiple reasoning steps
    before generating the final patch.
    """

    def __init__(
        self,
        domain: str = 'swe_bench',
        model_name: Optional[str] = None,
        num_reasoning_steps: int = 3,
        **kwargs,
    ):
        self.num_reasoning_steps = num_reasoning_steps
        super().__init__(domain=domain, model_name=model_name, **kwargs)

    def build_graph(self):
        """Build the ReAct agent graph with reasoning steps."""
        # Create reasoning steps
        reasoning_steps = []
        for i in range(self.num_reasoning_steps):
            step = CoTStep(
                domain=self.domain,
                model_name=self.model_name,
                step_name=f"reasoning_{i}",
            )
            self.add_node(step)
            reasoning_steps.append(step)

        # Create final answer node
        final_answer = DirectAnswer(self.domain, self.model_name)
        self.add_node(final_answer)

        # Connect reasoning steps sequentially
        for i in range(len(reasoning_steps) - 1):
            self.add_edge(reasoning_steps[i], reasoning_steps[i + 1])

        # Connect last reasoning step to final answer
        self.add_edge(reasoning_steps[-1], final_answer)

        # Set input and output nodes
        self.input_nodes = [reasoning_steps[0]]
        self.output_nodes = [final_answer]
