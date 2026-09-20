"""
File analysis operation for SWE-bench.

Provides file reading and analysis capabilities.
"""

from typing import List, Any, Optional, Dict
from pathlib import Path
import os
from swarm.llm.format import Message
from swarm.graph import Node
from swarm.utils.log import logger
from swarm.environment.prompt.prompt_set_registry import PromptSetRegistry
from swarm.llm import LLMRegistry


class FileAnalyse(Node):
    """
    File analysis operation for reading and analyzing code files.

    Can read specified files and provide context for patch generation.
    """

    def __init__(
        self,
        domain: str = 'swe_bench',
        model_name: Optional[str] = None,
        operation_description: str = "Read and analyze code files.",
        max_token: int = 4096,
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

    def _read_file(self, file_path: str, max_lines: int = 500) -> str:
        """
        Read content from a file.

        Args:
            file_path: Path to the file to read
            max_lines: Maximum number of lines to read

        Returns:
            File content as string
        """
        try:
            path = Path(file_path)
            if not path.exists():
                return f"Error: File not found: {file_path}"

            with open(path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            if len(lines) > max_lines:
                content = ''.join(lines[:max_lines])
                content += f"\n... (truncated, {len(lines) - max_lines} more lines)"
            else:
                content = ''.join(lines)

            return content

        except Exception as e:
            return f"Error reading file: {e}"

    def _read_files_from_paths(self, file_paths: List[str]) -> Dict[str, str]:
        """
        Read multiple files and return their contents.

        Args:
            file_paths: List of file paths to read

        Returns:
            Dictionary mapping file paths to their contents
        """
        results = {}
        for path in file_paths:
            results[path] = self._read_file(path)
        return results

    async def _execute(self, inputs: List[Any] = [], **kwargs):
        """Execute file analysis."""
        node_inputs = self.process_input(inputs)
        outputs = []

        for input in node_inputs:
            # Get file paths from input
            file_paths = input.get("file_paths", [])
            repo_path = input.get("repo_path", "")
            query = input.get("query", "")

            if not file_paths:
                # If no specific files, return empty analysis
                execution = {
                    "operation": self.node_name,
                    "input": input,
                    "files_read": {},
                    "analysis": "No files specified for analysis."
                }
                outputs.append(execution)
                self.memory.add(self.id, execution)
                continue

            # Read files
            files_content = {}
            for file_path in file_paths:
                if not os.path.isabs(file_path):
                    # Relative path - prepend repo path
                    full_path = os.path.join(repo_path, file_path)
                else:
                    full_path = file_path

                content = self._read_file(full_path)
                files_content[file_path] = content

            # Build analysis prompt
            role = self.prompt_set.get_role() if self.prompt_set else "code analyst"
            constraint = self.prompt_set.get_constraint() if self.prompt_set else (
                "Analyze the provided code files and summarize the relevant parts "
                "that need to be modified to fix the issue."
            )

            files_summary = "\n\n".join([
                f"=== {path} ===\n{content}"
                for path, content in files_content.items()
            ])

            prompt = f"""Based on the issue description: {query}

Analyze the following files and identify what changes are needed:

{files_summary}

Provide a summary of the relevant code sections and what modifications would be needed.
"""

            message = [
                Message(role="system", content=f"You are a {role}. {constraint}"),
                Message(role="user", content=prompt)
            ]

            response = await self.llm.agen(message, max_tokens=self.max_token)

            execution = {
                "operation": self.node_name,
                "input": input,
                "files_read": list(files_content.keys()),
                "files_content": files_content,
                "analysis": response,
            }
            outputs.append(execution)
            self.memory.add(self.id, execution)

        return outputs
