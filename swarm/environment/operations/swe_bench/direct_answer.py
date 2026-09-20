"""
SWE-bench Direct Answer Operation.

Generates patch/diff output for SWE-bench tasks.
"""

from typing import List, Any, Optional
from copy import deepcopy
from swarm.llm.format import Message
from swarm.graph import Node
from swarm.memory.memory import GlobalMemory
from swarm.utils.log import logger
from swarm.utils.globals import Cost
from swarm.environment.prompt.prompt_set_registry import PromptSetRegistry
from swarm.llm import LLMRegistry


class SWEDirectAnswer(Node):
    """
    Direct answer operation for SWE-bench.

    Generates a patch/diff as output based on the problem description.
    """

    def __init__(
        self,
        domain: str = 'swe_bench',
        model_name: Optional[str] = None,
        operation_description: str = "Generate a patch to fix the issue.",
        max_token: int = 4096,  # Higher token limit for patch generation
        id=None,
    ):
        super().__init__(operation_description, id, True)
        self.domain = domain
        self.model_name = model_name
        self.llm = LLMRegistry.get(model_name)
        self.max_token = max_token
        self.prompt_set = PromptSetRegistry.get(domain)

    @property
    def node_name(self):
        return self.__class__.__name__

    async def _execute(self, inputs: List[Any] = [], **kwargs):
        """Execute the direct answer operation."""
        node_inputs = self.process_input(inputs)
        outputs = []

        for input in node_inputs:
            task = input.get("task", "")
            instance_id = input.get("instance_id", "unknown")

            # Get role and constraint from prompt set
            role = self.prompt_set.get_role() if self.prompt_set else "software engineer"
            constraint = self.prompt_set.get_constraint() if self.prompt_set else (
                "Generate a precise patch/diff to fix the issue. "
                "Output only the diff in unified format, starting with 'diff --git'."
            )

            # Build prompt
            prompt = self.prompt_set.get_answer_prompt(question=task) if self.prompt_set else task

            message = [
                Message(
                    role="system",
                    content=f"You are a {role}. {constraint}"
                ),
                Message(role="user", content=prompt)
            ]

            response = await self.llm.agen(message, max_tokens=self.max_token)

            execution = {
                "operation": self.node_name,
                "task": task,
                "instance_id": instance_id,
                "input": task,
                "output": response,
                "metadata": input.get("metadata", {}),
                "format": "patch"
            }
            outputs.append(execution)
            self.memory.add(self.id, execution)

        return outputs
