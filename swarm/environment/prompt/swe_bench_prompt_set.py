"""
SWE-bench prompt set.

Provides prompts for SWE-bench tasks.
"""

from typing import Dict, Any
from swarm.environment.prompt.prompt_set import PromptSet
from swarm.environment.prompt.prompt_set_registry import PromptSetRegistry
from swarm.environment.prompt.common import get_combine_materials


@PromptSetRegistry.register('swe_bench')
class SWEBenchPromptSet(PromptSet):
    """
    Prompt set for SWE-bench tasks.

    Specializes in generating patches/diffs for bug fixes.
    """

    @staticmethod
    def get_role():
        return "software engineer"

    @staticmethod
    def get_constraint():
        return (
            "You are an expert software engineer tasked with fixing bugs in code repositories. "
            "Your goal is to generate a precise patch/diff that fixes the issue described. "
            "Output ONLY the unified diff format, starting with 'diff --git'. "
            "Do not include any explanations, markdown code blocks, or other text outside the diff. "
            "The diff should modify only the necessary files to fix the issue."
        )

    @staticmethod
    def get_format():
        return "patch"

    @staticmethod
    def get_answer_prompt(question):
        """
        Generate the prompt for patch generation.

        Args:
            question: The problem statement and context

        Returns:
            Formatted prompt for the LLM
        """
        return f"""{question}

## Task
Generate a unified diff patch that fixes the issue described above.

## Output Format
Output ONLY the diff in the following format:
```
diff --git a/path/to/file.py b/path/to/file.py
--- a/path/to/file.py
+++ b/path/to/file.py
@@ -old_line,old_count +new_line,new_count @@
 context line
-old line
+new line
```

## Important
1. Start directly with 'diff --git' - no preamble or explanation
2. Include all necessary changes to fix the issue
3. Maintain proper unified diff format
4. Do not include any text outside the diff

Generate the patch now:
"""

    @staticmethod
    def get_adversarial_answer_prompt(question):
        """Adversarial answer prompt - not used for SWE-bench."""
        return None

    @staticmethod
    def get_query_prompt(question):
        return (
            "# Information Gathering for Bug Fix\n\n"
            f"## Target Issue:\n{question}\n\n"
            "## Clues for Investigation:\n"
            "Identify critical clues and concepts within the issue that are essential for finding the solution.\n"
        )

    @staticmethod
    def get_file_analysis_prompt(query, file):
        return (
            "# File Analysis Task\n\n"
            f"## 🔍 Required Information:\n---\n{query}\n---\n\n"
            f"## 📄 File Under Analysis:\n---\n{file}\n---\n\n"
            "## Instructions:\n"
            "1. Identify the key sections in the file relevant to the query.\n"
            "2. Extract and summarize the necessary information.\n"
        )

    @staticmethod
    def get_websearch_prompt(question, query):
        return (
            "# Web Search Task\n\n"
            f"## Original Issue: \n---\n{question}\n---\n\n"
            f"## 🔍 Search Objective:\n---\n{query}\n---\n\n"
            "Generate three specific search queries related to the issue.\n"
            "Format: 'query1, query2, query3'"
        )

    @staticmethod
    def get_distill_websearch_prompt(question, query, results):
        return (
            "# Summarization of Search Results\n\n"
            f"## Original Issue: \n---\n{question}\n---\n\n"
            f"## 🔍 Required Information:\n---\n{query}\n---\n\n"
            f"## 🌐 Search Results:\n---\n{results}\n---\n\n"
            "Summarize the key findings relevant to solving the issue."
        )

    @staticmethod
    def get_reflect_prompt(question, answer):
        return (
            "# Reflection on Generated Patch\n\n"
            f"## Issue:\n---\n{question}\n---\n\n"
            f"## Generated Patch:\n---\n{answer}\n---\n\n"
            "Review the patch and verify:\n"
            "1. Is the diff format correct?\n"
            "2. Are all necessary changes included?\n"
            "3. Are there any obvious errors?\n"
        )

    @staticmethod
    def get_react_prompt(question, solutions, feedback):
        return (
            "# ReAct Reasoning for Bug Fix\n\n"
            f"## Issue: \n---\n{question}\n---\n\n"
            f"## Previous Attempts:\n---\n{solutions}\n---\n\n"
            f"## Feedback:\n---\n{feedback}\n---\n\n"
            "Analyze and propose the next step to fix the issue."
        )

    @staticmethod
    def get_combine_materials(materials: Dict[str, Any]) -> str:
        return get_combine_materials(materials)
