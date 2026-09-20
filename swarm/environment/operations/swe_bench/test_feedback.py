"""
SWE-bench Test Feedback Loop Operation (GPTSwarm-native).

After SearchReplaceEdit has modified files in the per-case container, this
operation closes the loop that one-shot generation lacks:

1. Asks the LLM for a small reproduction script derived from the issue text
   and runs it in the (network-isolated) container.
2. Runs a small subset of PASS_TO_PASS tests as a regression guard (these
   exist at base commit; FAIL_TO_PASS tests usually only exist after the
   golden test patch, so running them would leak the answer).
3. Feeds failures back to the LLM as SEARCH/REPLACE edits and iterates.

The graph stays a DAG: the iteration happens inside this node.
"""

import json
import re
import shlex
from typing import Any, Dict, List, Optional

from swarm.graph import Node
from swarm.llm import LLMRegistry
from swarm.llm.format import Message
from swarm.environment.prompt.prompt_set_registry import PromptSetRegistry
from swarm.environment.domain.swe_bench.env import (
    CONTAINER_WORKDIR,
    get_current_container,
    exec_in_container,
)
from swarm.environment.operations.swe_bench.code_edit import (
    EDIT_FORMAT_INSTRUCTIONS,
    parse_edit_blocks,
    apply_block,
    closest_region,
    _is_test_path,
)

MAX_ROUNDS = 3
MAX_REGRESSION_TESTS = 5
REPRO_TIMEOUT = 180
TEST_TIMEOUT = 420
OUTPUT_CAP = 6000

ACTIVATE = "source /opt/miniconda3/bin/activate testbed 2>/dev/null || true; "


def _bash(container: str, cmd: str, timeout: int):
    return exec_in_container(
        container,
        ["bash", "-c", ACTIVATE + f"cd {CONTAINER_WORKDIR} && " + cmd],
        timeout=timeout,
    )


def build_test_command(repo: str, test_ids: List[str]) -> Optional[str]:
    """Build the container command to run the given test ids for a repo.
    Returns None when no runnable command can be built."""
    if not test_ids:
        return None
    if repo == "django/django":
        ids = []
        for t in test_ids:
            m = re.match(r"(\S+) \(([\w.]+)\)", t)
            ids.append(f"{m.group(2)}.{m.group(1)}" if m else t)
        return ("python ./tests/runtests.py --parallel 1 --verbosity 1 "
                + " ".join(shlex.quote(i) for i in ids))
    if repo == "sympy/sympy":
        # bare function names: locate their test files first (whole-repo pytest
        # collection is far too slow), then filter with -k
        names = [t for t in test_ids if re.match(r"^[\w-]+$", t)]
        if not names:
            return None
        greps = " ".join(
            f"$(grep -rl 'def {n}(' sympy --include='test_*.py' | head -1)"
            for n in names[:3])
        expr = " or ".join(names[:3])
        return f"python -m pytest -q --no-header {greps} -k {shlex.quote(expr)}"
    # pytest node ids (matplotlib, sphinx, ...)
    return ("python -m pytest -q --no-header "
            + " ".join(shlex.quote(t) for t in test_ids))


class TestFeedbackLoop(Node):
    """Iteratively verify and repair the edits made by SearchReplaceEdit."""

    def __init__(
        self,
        domain: str,
        model_name: Optional[str],
        operation_description: str = "Verify edits with tests and repair iteratively.",
        max_token: int = 8192,
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

    # ------------- container helpers -------------

    def _git_diff(self, container: str) -> str:
        r = exec_in_container(
            container, ["git", "-C", CONTAINER_WORKDIR, "diff", "HEAD"], timeout=60)
        return r.stdout if r.returncode == 0 else ""

    def _read_file(self, container: str, path: str) -> Optional[str]:
        r = exec_in_container(
            container, ["cat", f"{CONTAINER_WORKDIR}/{path}"], timeout=60)
        return r.stdout if r.returncode == 0 else None

    def _write_file(self, container: str, path: str, content: str) -> bool:
        r = exec_in_container(
            container, ["bash", "-c", f"cat > {shlex.quote(path)}"],
            timeout=60, input_text=content)
        return r.returncode == 0

    def _apply_edit_blocks(self, container: str, blocks) -> List[str]:
        applied = []
        for path, search, replace in blocks:
            path = path.lstrip("./")
            if _is_test_path(path):
                continue
            content = self._read_file(container, path)
            if content is None:
                continue
            new_content = apply_block(content, search, replace)
            if new_content is None:
                continue
            if self._write_file(container, f"{CONTAINER_WORKDIR}/{path}", new_content):
                applied.append(path)
        return applied

    # ------------- verification steps -------------

    async def _make_repro_script(self, task: str, diff: str) -> str:
        prompt = (
            f"{task}\n\n"
            f"## Current fix (git diff)\n```diff\n{diff[:8000]}\n```\n\n"
            "## Your task\n"
            "Write a SHORT standalone python script that reproduces the issue "
            "described above and verifies whether the fix works.\n"
            "Requirements:\n"
            "- The script runs inside the repository root and may import the "
            "package directly (it is installed in development mode).\n"
            "- If the repository is django, call django.conf.settings.configure() "
            "and django.setup() as needed before importing models.\n"
            "- Print 'REPRO_PASS' if the buggy behavior is FIXED, "
            "'REPRO_FAIL: <reason>' otherwise. Never raise uncaught exceptions.\n"
            "- No network access, no file downloads, finish within 60 seconds.\n"
            "Output ONLY the python code, no markdown fences."
        )
        resp = await self.llm.agen(
            [Message(role="system",
                     content="You are an expert software engineer writing a "
                             "minimal reproduction/verification script."),
             Message(role="user", content=prompt)],
            max_tokens=2000,
        )
        code = str(resp)
        if "```" in code:
            m = re.search(r"```(?:python)?\n(.*?)```", code, re.DOTALL)
            if m:
                code = m.group(1)
        return code

    def _run_repro(self, container: str, script: str) -> str:
        if not self._write_file(container, "/tmp/repro.py", script):
            return "could not write repro script"
        r = _bash(container, "python /tmp/repro.py 2>&1", REPRO_TIMEOUT)
        return (r.stdout or "")[-OUTPUT_CAP:]

    def _run_regressions(self, container: str, repo: str,
                         pass_to_pass: List[str]) -> str:
        tests = pass_to_pass[:MAX_REGRESSION_TESTS]
        cmd = build_test_command(repo, tests)
        if cmd is None:
            return ""
        try:
            r = _bash(container, cmd + " 2>&1", TEST_TIMEOUT)
        except Exception:
            return ""
        out = (r.stdout or "")
        # Only exit codes 0/1 are meaningful test outcomes; anything else
        # (collection/usage errors, missing runner) means the guard is
        # unusable — better no guard than a spurious "regression".
        if r.returncode not in (0, 1):
            return ""
        return f"exit={r.returncode}\n{out[-OUTPUT_CAP:]}"

    @staticmethod
    def _regressions_ok(regression_output: str) -> bool:
        if not regression_output:
            return True  # no runnable regression guard
        return regression_output.startswith("exit=0")

    # ------------- main -------------

    async def _execute(self, inputs: List[Any] = [], **kwargs):
        node_inputs = self.process_input(inputs)
        outputs = []

        for input in node_inputs:
            task = input.get("task", "")
            container = input.get("container") or get_current_container()
            metadata = input.get("metadata", {}) or {}
            repo = input.get("repo", "")
            p2p = metadata.get("PASS_TO_PASS", [])
            if isinstance(p2p, str):
                try:
                    p2p = json.loads(p2p)
                except Exception:
                    p2p = []
            log = []

            if container is None:
                execution = {"operation": self.node_name, "task": task,
                             "output": "Error: no container", "format": "text"}
                outputs.append(execution)
                self.memory.add(self.id, execution)
                continue

            diff = self._git_diff(container)
            repro_script = None
            repro_out = ""
            regression_out = ""

            for round_no in range(1, MAX_ROUNDS + 1):
                diff = self._git_diff(container)
                if not diff.strip():
                    log.append(f"round {round_no}: no edits present, skipping verification")
                    break
                if repro_script is None:
                    try:
                        repro_script = await self._make_repro_script(task, diff)
                    except Exception as e:
                        log.append(f"repro generation failed: {e}")
                        break
                repro_out = self._run_repro(container, repro_script)
                regression_out = self._run_regressions(container, repo, p2p)
                repro_ok = "REPRO_PASS" in repro_out
                reg_ok = self._regressions_ok(regression_out)
                log.append(f"round {round_no}: repro_ok={repro_ok} regression_ok={reg_ok}")
                if repro_ok and reg_ok:
                    break
                if round_no == MAX_ROUNDS:
                    break

                # Ask for repair edits
                repair_prompt = (
                    f"{task}\n\n"
                    f"## Current fix (git diff)\n```diff\n{diff[:8000]}\n```\n\n"
                    f"## Verification script output\n```\n{repro_out}\n```\n\n"
                    + (f"## Regression tests output (must stay passing)\n"
                       f"```\n{regression_out}\n```\n\n" if regression_out else "")
                    + "## Your task\n"
                    "The current fix does not fully work. Improve it.\n"
                    "If the verification script itself is wrong (e.g. bad "
                    "imports or setup), output a corrected script in a "
                    "```python fenced block INSTEAD of edit blocks.\n"
                    "Otherwise, modify the SOURCE files with edit blocks. "
                    "To fetch exact current lines for a SEARCH block, rely on "
                    "the diff context above.\n\n"
                    + EDIT_FORMAT_INSTRUCTIONS
                )
                try:
                    resp = await self.llm.agen(
                        [Message(role="system",
                                 content="You are an expert software engineer "
                                         "iterating on a bug fix using test feedback."),
                         Message(role="user", content=repair_prompt)],
                        max_tokens=self.max_token,
                    )
                except Exception as e:
                    log.append(f"repair call failed: {e}")
                    break
                resp = str(resp)
                blocks = parse_edit_blocks(resp)
                if blocks:
                    # SEARCH text may quote from the diff; also try against files
                    applied = self._apply_edit_blocks(container, blocks)
                    log.append(f"round {round_no}: applied repair edits to {applied}")
                    if not applied:
                        break
                else:
                    m = re.search(r"```python\n(.*?)```", resp, re.DOTALL)
                    if m:
                        repro_script = m.group(1)
                        log.append(f"round {round_no}: repro script replaced")
                    else:
                        log.append(f"round {round_no}: no actionable repair output")
                        break

            final_diff = self._git_diff(container)
            execution = {
                "operation": self.node_name,
                "task": task,
                "container": container,
                "repo": repo,
                "rounds_log": log,
                "output": "; ".join(log) or "no verification performed",
                "format": "text",
            }
            outputs.append(execution)
            self.memory.add(self.id, execution)

        return outputs
