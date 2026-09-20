"""
SWE-bench File Operations.

All operations interact with the per-case SWE-bench docker container
(the target repository lives at /testbed inside the container) via
`docker exec`. The container name is taken from the input dict
("container" key) or from the module-level current container set by the
runner (see swarm.environment.domain.swe_bench.env).
"""

from typing import List, Any, Optional, Dict
from swarm.graph import Node
from swarm.memory.memory import GlobalMemory
from swarm.environment.prompt.prompt_set_registry import PromptSetRegistry
from swarm.llm import LLMRegistry
from swarm.environment.domain.swe_bench.env import (
    CONTAINER_WORKDIR,
    get_current_container,
    exec_in_container,
)

MAX_CONTEXT_CHARS = 8000


def _extract_patch(text: str) -> str:
    """Extract a unified diff from LLM output, stripping markdown fences."""
    if not isinstance(text, str) or not text:
        return ""
    if "```" in text:
        import re as _re
        blocks = _re.findall(r"```(?:diff|patch)?\n(.*?)```", text, _re.DOTALL)
        for block in blocks:
            if "diff --git" in block or block.lstrip().startswith("---"):
                text = block
                break
    idx = text.find("diff --git")
    if idx != -1:
        return text[idx:].strip()
    stripped = text.lstrip()
    if stripped.startswith("---"):
        return stripped.strip()
    return ""


def _resolve_container(input: Dict[str, Any]) -> Optional[str]:
    """Resolve the docker container name for this task."""
    return input.get("container") or get_current_container()


class FileRead(Node):
    """
    Reads file content from the repository inside the case container.
    If no files are specified, lists the repository files as context.
    """

    def __init__(
        self,
        domain: str,
        model_name: Optional[str],
        operation_description: str = "Read file content from repository.",
        id=None,
    ):
        super().__init__(operation_description, id, True)
        self.domain = domain
        self.model_name = model_name
        self.llm = LLMRegistry.get(model_name)
        self.prompt_set = PromptSetRegistry.get(domain)

    @property
    def node_name(self):
        return self.__class__.__name__

    async def _execute(self, inputs: List[Any] = [], **kwargs) -> List[Dict[str, Any]]:
        node_inputs = self.process_input(inputs)
        outputs = []

        for input in node_inputs:
            task = input.get("task", "")
            container = _resolve_container(input)
            files_to_read = input.get("files", []) or input.get("files_to_read", [])

            if container is None:
                output_text = "Error: no container available for this task"
            elif files_to_read:
                read_results = []
                for file_path in files_to_read:
                    try:
                        result = exec_in_container(
                            container,
                            ["cat", f"{CONTAINER_WORKDIR}/{file_path}"],
                            timeout=60,
                        )
                        if result.returncode == 0:
                            read_results.append(
                                f"=== {file_path} ===\n{result.stdout[:MAX_CONTEXT_CHARS]}")
                        else:
                            read_results.append(
                                f"=== {file_path} ===\nError: {result.stderr.strip()}")
                    except Exception as e:
                        read_results.append(f"=== {file_path} ===\nError: {e}")
                output_text = "\n\n".join(read_results)
            else:
                # No files specified: provide repository layout as context
                try:
                    result = exec_in_container(
                        container,
                        ["bash", "-c",
                         f"cd {CONTAINER_WORKDIR} && git ls-files | head -200"],
                        timeout=60,
                    )
                    output_text = "Repository files (partial):\n" + result.stdout
                except Exception as e:
                    output_text = f"Error listing repository files: {e}"

            output_text = output_text[:MAX_CONTEXT_CHARS]

            execution = {
                "operation": self.node_name,
                "task": f"{task}\n\n## Repository Context\n{output_text}",
                "files": files_to_read,
                "container": container,
                "output": output_text,
                "format": "file contents",
            }
            outputs.append(execution)
            self.memory.add(self.id, execution)

        return outputs


class FileWrite(Node):
    """
    Writes content to files in the repository inside the case container.
    """

    def __init__(
        self,
        domain: str,
        model_name: Optional[str],
        operation_description: str = "Write content to file in repository.",
        id=None,
    ):
        super().__init__(operation_description, id, True)
        self.domain = domain
        self.model_name = model_name
        self.llm = LLMRegistry.get(model_name)
        self.prompt_set = PromptSetRegistry.get(domain)

    @property
    def node_name(self):
        return self.__class__.__name__

    async def _execute(self, inputs: List[Any] = [], **kwargs) -> List[Dict[str, Any]]:
        node_inputs = self.process_input(inputs)
        outputs = []

        for input in node_inputs:
            task = input.get("task", "")
            container = _resolve_container(input)
            writes = input.get("writes", [])  # List of {"path": str, "content": str}

            write_results = []
            for write in writes:
                file_path = write.get("path", "")
                content = write.get("content", "")
                if container is None:
                    write_results.append({"path": file_path, "status": "error",
                                          "error": "no container"})
                    continue
                try:
                    target = f"{CONTAINER_WORKDIR}/{file_path}"
                    result = exec_in_container(
                        container,
                        ["bash", "-c",
                         f"mkdir -p \"$(dirname '{target}')\" && cat > '{target}'"],
                        timeout=60,
                        input_text=content,
                    )
                    status = "success" if result.returncode == 0 else "error"
                    write_results.append({"path": file_path, "status": status,
                                          "error": result.stderr.strip() or None})
                except Exception as e:
                    write_results.append({"path": file_path, "status": "error",
                                          "error": str(e)})

            n_ok = len([r for r in write_results if r["status"] == "success"])
            execution = {
                "operation": self.node_name,
                "task": task,
                "writes": writes,
                "container": container,
                "write_results": write_results,
                "output": f"Wrote {n_ok} files successfully",
                "format": "text",
            }
            outputs.append(execution)
            self.memory.add(self.id, execution)

        return outputs


class BashCommand(Node):
    """
    Executes bash commands in the case container (cwd: /testbed).
    """

    def __init__(
        self,
        domain: str,
        model_name: Optional[str],
        operation_description: str = "Execute bash command in repository.",
        id=None,
    ):
        super().__init__(operation_description, id, True)
        self.domain = domain
        self.model_name = model_name
        self.llm = LLMRegistry.get(model_name)
        self.prompt_set = PromptSetRegistry.get(domain)

    @property
    def node_name(self):
        return self.__class__.__name__

    async def _execute(self, inputs: List[Any] = [], **kwargs) -> List[Dict[str, Any]]:
        node_inputs = self.process_input(inputs)
        outputs = []

        for input in node_inputs:
            task = input.get("task", "")
            container = _resolve_container(input)
            commands = input.get("commands", [])

            command_results = []
            for cmd in commands:
                if container is None:
                    command_results.append(f"$ {cmd}\nError: no container")
                    continue
                try:
                    result = exec_in_container(
                        container,
                        ["bash", "-c", f"cd {CONTAINER_WORKDIR} && {cmd}"],
                        timeout=120,
                    )
                    out = result.stdout[:MAX_CONTEXT_CHARS]
                    if result.returncode != 0:
                        out += f"\n[stderr] {result.stderr[:2000]}"
                    command_results.append(f"$ {cmd}\n{out}")
                except Exception as e:
                    command_results.append(f"$ {cmd}\nError: {e}")

            output_text = "\n\n".join(command_results)[:MAX_CONTEXT_CHARS]

            execution = {
                "operation": self.node_name,
                "task": task,
                "commands": commands,
                "container": container,
                "output": output_text,
                "format": "command output",
            }
            outputs.append(execution)
            self.memory.add(self.id, execution)

        return outputs


class GrepSearch(Node):
    """
    Searches for patterns in repository files inside the case container.
    """

    def __init__(
        self,
        domain: str,
        model_name: Optional[str],
        operation_description: str = "Search for pattern in repository files.",
        id=None,
    ):
        super().__init__(operation_description, id, True)
        self.domain = domain
        self.model_name = model_name
        self.llm = LLMRegistry.get(model_name)
        self.prompt_set = PromptSetRegistry.get(domain)

    @property
    def node_name(self):
        return self.__class__.__name__

    async def _execute(self, inputs: List[Any] = [], **kwargs) -> List[Dict[str, Any]]:
        node_inputs = self.process_input(inputs)
        outputs = []

        for input in node_inputs:
            task = input.get("task", "")
            container = _resolve_container(input)
            patterns = input.get("patterns", [])

            search_results = []
            for pattern in patterns:
                if container is None:
                    search_results.append(f"Pattern: {pattern}\nError: no container")
                    continue
                try:
                    result = exec_in_container(
                        container,
                        ["bash", "-c",
                         f"cd {CONTAINER_WORKDIR} && grep -rn -- {pattern!r} . | head -100"],
                        timeout=60,
                    )
                    search_results.append(f"Pattern: {pattern}\n{result.stdout}")
                except Exception as e:
                    search_results.append(f"Pattern: {pattern}\nError: {e}")

            output_text = "\n\n".join(search_results)[:MAX_CONTEXT_CHARS]
            new_task = task
            if output_text:
                new_task = f"{task}\n\n## Search Results\n{output_text}"

            execution = {
                "operation": self.node_name,
                "task": new_task,
                "patterns": patterns,
                "container": container,
                "output": output_text,
                "format": "search results",
            }
            outputs.append(execution)
            self.memory.add(self.id, execution)

        return outputs


class GitDiff(Node):
    """
    Extracts the final patch.

    Runs `git diff HEAD` inside the case container. If the repository has no
    changes (agents that only propose a patch textually), falls back to the
    candidate patch produced by the predecessor node.
    """

    def __init__(
        self,
        domain: str,
        model_name: Optional[str],
        operation_description: str = "Get git diff from repository.",
        id=None,
    ):
        super().__init__(operation_description, id, True)
        self.domain = domain
        self.model_name = model_name
        self.llm = LLMRegistry.get(model_name)
        self.prompt_set = PromptSetRegistry.get(domain)

    @property
    def node_name(self):
        return self.__class__.__name__

    async def _execute(self, inputs: List[Any] = [], **kwargs) -> List[Dict[str, Any]]:
        node_inputs = self.process_input(inputs)
        outputs = []

        for input in node_inputs:
            task = input.get("task", "")
            container = _resolve_container(input)

            diff_output = ""
            error = None
            if container is not None:
                try:
                    result = exec_in_container(
                        container,
                        ["git", "-C", CONTAINER_WORKDIR, "diff", "HEAD"],
                        timeout=60,
                    )
                    diff_output = result.stdout.strip()
                except Exception as e:
                    error = str(e)

            if not diff_output:
                # Fall back to the candidate patch from the predecessor output
                diff_output = _extract_patch(input.get("output", ""))

            execution = {
                "operation": self.node_name,
                "task": task,
                "container": container,
                "diff": diff_output,
                "output": diff_output if diff_output else (error or "No changes"),
                "format": "patch",
            }
            outputs.append(execution)
            self.memory.add(self.id, execution)

        return outputs
