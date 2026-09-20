"""
SWE-bench Agent with Code Analysis.

An agent that can:
1. Read code files from repository
2. Analyze the code to understand the issue
3. Generate and apply patches
4. Extract git diff as final patch
"""

from typing import Optional, List, Dict, Any
from swarm.graph import Graph
from swarm.environment.operations.swe_bench.file_ops import FileRead, BashCommand, GitDiff
from swarm.environment.operations.swe_bench.code_edit import SearchReplaceEdit
from swarm.environment.operations.swe_bench.test_feedback import TestFeedbackLoop
from swarm.environment.operations.direct_answer import DirectAnswer
from swarm.environment.agents.agent_registry import AgentRegistry


@AgentRegistry.register('SWERepairAgent')
class SWERepairAgent(Graph):
    """
    GPTSwarm-native SWE-bench agent with a verification loop.

    Workflow (DAG; iteration happens inside the nodes):
    1. SearchReplaceEdit: read real files, apply SEARCH/REPLACE edits
       (retries when nothing applies).
    2. TestFeedbackLoop: generate a reproduction script, run it plus a
       PASS_TO_PASS regression subset in the network-isolated container,
       feed failures back to the LLM and repair (up to 3 rounds).
    3. GitDiff: extract the final `git diff`.
    """

    def __init__(
        self,
        domain: str = 'swe_bench',
        model_name: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(domain=domain, model_name=model_name, **kwargs)

    def build_graph(self):
        edit = SearchReplaceEdit(self.domain, self.model_name)
        verify = TestFeedbackLoop(self.domain, self.model_name)
        git_diff = GitDiff(self.domain, self.model_name)
        for node in [edit, verify, git_diff]:
            self.add_node(node)
        edit.add_successor(verify)
        verify.add_successor(git_diff)
        self.input_nodes = [edit]
        self.output_nodes = [git_diff]


@AgentRegistry.register('SWEEditAgent')
class SWEEditAgent(Graph):
    """
    SWE-bench agent that edits real files instead of hand-writing diffs.

    Workflow:
    1. SearchReplaceEdit: locate files, read real content, obtain
       SEARCH/REPLACE edits from the LLM and apply them in the container
       (with one repair round and a syntax check).
    2. GitDiff: extract the resulting `git diff` — valid by construction.
    """

    def __init__(
        self,
        domain: str = 'swe_bench',
        model_name: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(domain=domain, model_name=model_name, **kwargs)

    def build_graph(self):
        edit = SearchReplaceEdit(self.domain, self.model_name)
        git_diff = GitDiff(self.domain, self.model_name)
        self.add_node(edit)
        self.add_node(git_diff)
        edit.add_successor(git_diff)
        self.input_nodes = [edit]
        self.output_nodes = [git_diff]


@AgentRegistry.register('SWECodeAgent')
class SWECodeAgent(Graph):
    """
    SWE-bench agent with code reading capabilities.

    Workflow:
    1. FileRead: Read relevant files based on problem statement
    2. DirectAnswer: Analyze and generate patch based on code content
    3. GitDiff: Extract final diff from repository changes

    This agent operates within the container, reading and modifying
    code files directly.
    """

    def __init__(
        self,
        domain: str = 'swe_bench',
        model_name: Optional[str] = None,
        files_to_read: Optional[List[str]] = None,
        **kwargs,
    ):
        self.files_to_read = files_to_read or []
        super().__init__(domain=domain, model_name=model_name, **kwargs)

    def build_graph(self):
        """Build the agent graph with file reading and analysis."""
        # File reading operation
        file_read = FileRead(self.domain, self.model_name)
        self.add_node(file_read)

        # Direct answer operation (generates patch)
        direct_answer = DirectAnswer(self.domain, self.model_name, max_token=8192)
        self.add_node(direct_answer)

        # Git diff operation (extracts final patch)
        git_diff = GitDiff(self.domain, self.model_name)
        self.add_node(git_diff)

        # Set up node connections
        file_read.add_successor(direct_answer)
        direct_answer.add_successor(git_diff)

        # Input/output nodes
        self.input_nodes = [file_read]
        self.output_nodes = [git_diff]


@AgentRegistry.register('SWEReActCodeAgent')
class SWEReActCodeAgent(Graph):
    """
    SWE-bench ReAct agent with iterative code analysis.

    Workflow:
    1. Think: Analyze the problem and plan actions
    2. FileRead/BashCommand: Execute planned actions
    3. Observe: Analyze results
    4. Repeat until patch is generated

    This agent can perform multiple iterations of:
    - Reading files
    - Searching code
    - Running tests
    - Generating patches
    """

    def __init__(
        self,
        domain: str = 'swe_bench',
        model_name: Optional[str] = None,
        max_iterations: int = 5,
        **kwargs,
    ):
        self.max_iterations = max_iterations
        super().__init__(domain=domain, model_name=model_name, **kwargs)

    def build_graph(self):
        """Build the ReAct-style agent graph (DAG; framework requires acyclic graphs)."""
        # Operations
        file_read = FileRead(self.domain, self.model_name)
        bash = BashCommand(self.domain, self.model_name)
        direct_answer = DirectAnswer(self.domain, self.model_name, max_token=8192)
        git_diff = GitDiff(self.domain, self.model_name)

        for node in [file_read, bash, direct_answer, git_diff]:
            self.add_node(node)

        # DAG connections: read -> bash -> answer -> diff, with a skip edge read -> answer
        file_read.add_successor(bash)
        file_read.add_successor(direct_answer)
        bash.add_successor(direct_answer)
        direct_answer.add_successor(git_diff)

        self.input_nodes = [file_read]
        self.output_nodes = [git_diff]


@AgentRegistry.register('SWEMultiStepAgent')
class SWEMultiStepAgent(Graph):
    """
    SWE-bench multi-step agent for complex bug fixes.

    Workflow:
    1. Search: Grep for relevant code patterns
    2. Read: Read identified files
    3. Analyze: Understand the issue
    4. Patch: Generate patch
    5. Verify: Get git diff

    This agent is suitable for bugs that require:
    - Understanding multiple files
    - Searching for specific patterns
    - Multiple iterations of code reading
    """

    def __init__(
        self,
        domain: str = 'swe_bench',
        model_name: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(domain=domain, model_name=model_name, **kwargs)

    def build_graph(self):
        """Build multi-step agent graph."""
        from swarm.environment.operations.swe_bench.file_ops import GrepSearch

        # Operations
        grep = GrepSearch(self.domain, self.model_name)
        file_read = FileRead(self.domain, self.model_name)
        direct_answer = DirectAnswer(self.domain, self.model_name, max_token=8192)
        git_diff = GitDiff(self.domain, self.model_name)

        for node in [grep, file_read, direct_answer, git_diff]:
            self.add_node(node)

        # Sequential connections
        grep.add_successor(file_read)
        file_read.add_successor(direct_answer)
        direct_answer.add_successor(git_diff)

        self.input_nodes = [grep]
        self.output_nodes = [git_diff]
