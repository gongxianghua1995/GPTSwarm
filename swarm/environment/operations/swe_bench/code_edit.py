"""
SWE-bench Search/Replace Edit Operation.

Instead of asking the LLM to hand-write a unified diff (which produced
malformed or hallucinated patches), this operation:

1. Locates candidate files (paths mentioned in the issue + an LLM pick
   over the repository listing).
2. Reads the REAL file content from the per-case container.
3. Asks the LLM for SEARCH/REPLACE edit blocks copied from that content.
4. Applies the blocks to the files inside the container (with whitespace-
   tolerant matching), giving the LLM one repair round for blocks that
   fail to match.
5. Syntax-checks edited Python files and reverts any file that no longer
   compiles.

The final patch is then extracted by the downstream GitDiff node as a real
`git diff`, which is valid by construction.
"""

import json
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from swarm.graph import Node
from swarm.llm import LLMRegistry
from swarm.llm.format import Message
from swarm.environment.prompt.prompt_set_registry import PromptSetRegistry
from swarm.environment.domain.swe_bench.env import (
    CONTAINER_WORKDIR,
    get_current_container,
    exec_in_container,
)

MAX_FILES = 3
MAX_FILE_CHARS = 30000
MAX_LISTING = 300

EDIT_FORMAT_INSTRUCTIONS = """\
Propose your code changes as one or more edit blocks in EXACTLY this format:

### edit: path/to/file.py
<<<<<<< SEARCH
lines copied EXACTLY from the current file content shown above
=======
the replacement lines
>>>>>>> REPLACE

Rules:
- The SEARCH text must be copied character-for-character (including
  indentation) from the file content provided above. Never write code from
  memory into a SEARCH block.
- Keep each SEARCH block small (under 25 lines) but include enough
  surrounding lines to make it unique within the file.
- Use several small edit blocks rather than one large one.
- Only modify source files. Do NOT modify test files.
- Output ONLY edit blocks, no other commentary."""


def parse_edit_blocks(text: str) -> List[Tuple[str, str, str]]:
    """Parse '### edit: path' + SEARCH/REPLACE blocks -> [(path, search, replace)]."""
    blocks = []
    path = None
    state = "idle"  # idle | search | replace
    search_lines: List[str] = []
    replace_lines: List[str] = []
    for line in text.split("\n"):
        header = re.match(r"^#{1,4}\s*(?:edit:|file:)?\s*([\w./+-]+\.\w+)\s*$", line.strip())
        if state == "idle":
            if header:
                path = header.group(1)
            elif line.startswith("<<<<<<") and path:
                state = "search"
                search_lines = []
        elif state == "search":
            if line.startswith("======="):
                state = "replace"
                replace_lines = []
            else:
                search_lines.append(line)
        elif state == "replace":
            if line.startswith(">>>>>>"):
                state = "idle"
                blocks.append((path, "\n".join(search_lines), "\n".join(replace_lines)))
            else:
                replace_lines.append(line)
    return blocks


def apply_block(content: str, search: str, replace: str) -> Optional[str]:
    """Apply one search/replace to content; whitespace-tolerant. None if no match."""
    search = search.strip("\n")
    replace = replace.strip("\n")
    if not search:
        return None
    if search in content:
        return content.replace(search, replace, 1)
    c_lines = content.split("\n")
    s_lines = search.split("\n")
    r_lines = replace.split("\n")
    for norm in (str.rstrip, str.strip):
        nc = [norm(l) for l in c_lines]
        ns = [norm(l) for l in s_lines]
        for i in range(len(nc) - len(ns) + 1):
            if nc[i:i + len(ns)] == ns:
                return "\n".join(c_lines[:i] + r_lines + c_lines[i + len(ns):])
    return None


def closest_region(content: str, search: str, radius: int = 6) -> str:
    """Return the region of content most similar to search, for repair prompts."""
    c_lines = content.split("\n")
    s_lines = [l.strip() for l in search.split("\n") if l.strip()]
    if not s_lines:
        return ""
    sm = SequenceMatcher(None, s_lines, [l.strip() for l in c_lines], autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    if not blocks:
        return "\n".join(c_lines[:2 * radius])
    lo = max(0, min(b.b for b in blocks) - radius)
    hi = min(len(c_lines), max(b.b + b.size for b in blocks) + radius)
    return "\n".join(c_lines[lo:hi])


def _is_test_path(path: str) -> bool:
    p = path.lower()
    base = p.rsplit("/", 1)[-1]
    return ("/tests/" in p or "/test/" in p or p.startswith("tests/")
            or base.startswith("test_") or base.endswith("_test.py"))


class SearchReplaceEdit(Node):
    """Reads real files, obtains SEARCH/REPLACE edits from the LLM and applies
    them inside the case container."""

    def __init__(
        self,
        domain: str,
        model_name: Optional[str],
        operation_description: str = "Edit repository files via search/replace blocks.",
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

    # ---------------- container file helpers ----------------

    def _read_file(self, container: str, path: str) -> Optional[str]:
        try:
            result = exec_in_container(
                container, ["cat", f"{CONTAINER_WORKDIR}/{path}"], timeout=60)
            return result.stdout if result.returncode == 0 else None
        except Exception:
            return None

    def _write_file(self, container: str, path: str, content: str) -> bool:
        try:
            target = f"{CONTAINER_WORKDIR}/{path}"
            result = exec_in_container(
                container,
                ["bash", "-c", f"cat > '{target}'"],
                timeout=60,
                input_text=content,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _list_py_files(self, container: str) -> List[str]:
        try:
            result = exec_in_container(
                container,
                ["bash", "-c",
                 f"cd {CONTAINER_WORKDIR} && git ls-files '*.py'"],
                timeout=60,
            )
            files = [f for f in result.stdout.split("\n") if f.strip()]
            return [f for f in files if not _is_test_path(f)]
        except Exception:
            return []

    # ---------------- steps ----------------

    async def _locate_files(self, task: str, container: str,
                            listing: List[str]) -> List[str]:
        existing = set(listing)
        mentioned = []
        for cand in re.findall(r"[\w./-]+\.py", task):
            cand = cand.lstrip("./")
            if cand in existing and cand not in mentioned:
                mentioned.append(cand)

        shown = listing[:MAX_LISTING]
        prompt = (
            f"{task}\n\n"
            f"## Repository python files\n" + "\n".join(shown) + "\n\n"
            "## Question\n"
            f"Which files (at most {MAX_FILES}) most likely need to be MODIFIED "
            "to fix this issue? Consider the files mentioned in the issue: "
            f"{mentioned or 'none'}.\n"
            "Respond with ONLY a JSON array of file paths, e.g. "
            '["pkg/module.py"]. No other text.'
        )
        try:
            response = await self.llm.agen(
                [Message(role="system",
                         content="You are an expert software engineer locating "
                                 "the files that must be changed to fix a bug."),
                 Message(role="user", content=prompt)],
                max_tokens=400,
            )
            picked = [p.lstrip("./") for p in re.findall(r"[\w./-]+\.py", str(response))]
        except Exception:
            picked = []

        files = []
        for p in picked + mentioned:
            if p in existing and not _is_test_path(p) and p not in files:
                files.append(p)
            if len(files) >= MAX_FILES:
                break
        return files

    def _render_file(self, path: str, content: str, task: str) -> str:
        if len(content) <= MAX_FILE_CHARS:
            return f"### file: {path}\n```python\n{content}\n```"
        # Oversized file: show regions around issue keywords plus the head.
        lines = content.split("\n")
        tokens = set(t for t in re.findall(r"[A-Za-z_]\w{4,}", task))
        hit_idx = [i for i, l in enumerate(lines) if any(t in l for t in tokens)]
        keep = set(range(0, 120))
        for i in hit_idx:
            keep.update(range(max(0, i - 30), min(len(lines), i + 31)))
        rendered = []
        budget = MAX_FILE_CHARS
        last = -2
        for i in sorted(keep):
            if i != last + 1:
                rendered.append(f"... (lines {last + 2}-{i} omitted) ...")
            rendered.append(lines[i])
            last = i
            budget -= len(lines[i]) + 1
            if budget <= 0:
                rendered.append("... (truncated) ...")
                break
        body = "\n".join(rendered)
        return (f"### file: {path} (excerpt; unshown parts exist)\n"
                f"```python\n{body}\n```")

    def _apply_blocks(
        self, container: str, blocks: List[Tuple[str, str, str]],
        allowed_files: List[str],
    ) -> Tuple[List[str], List[Tuple[str, str, str]]]:
        """Apply blocks in the container. Returns (edited_files, failed_blocks)."""
        edited: List[str] = []
        failed: List[Tuple[str, str, str]] = []
        cache: Dict[str, Optional[str]] = {}
        for path, search, replace in blocks:
            path = path.lstrip("./")
            if _is_test_path(path):
                continue
            if path not in cache:
                cache[path] = self._read_file(container, path)
            content = cache[path]
            if content is None:
                failed.append((path, search, replace))
                continue
            new_content = apply_block(content, search, replace)
            if new_content is None:
                failed.append((path, search, replace))
                continue
            cache[path] = new_content
            if path not in edited:
                edited.append(path)
        for path in edited:
            self._write_file(container, path, cache[path])
        return edited, failed

    def _syntax_check(self, container: str, files: List[str]) -> List[str]:
        """py_compile edited files; revert files that fail. Returns reverted."""
        reverted = []
        for path in files:
            if not path.endswith(".py"):
                continue
            try:
                result = exec_in_container(
                    container,
                    ["bash", "-c",
                     f"cd {CONTAINER_WORKDIR} && "
                     f"(command -v python >/dev/null && python -m py_compile '{path}' "
                     f"|| python3 -m py_compile '{path}')"],
                    timeout=60,
                )
                if result.returncode != 0:
                    exec_in_container(
                        container,
                        ["git", "-C", CONTAINER_WORKDIR, "checkout", "--", path],
                        timeout=60,
                    )
                    reverted.append(path)
            except Exception:
                pass
        return reverted

    async def _execute(self, inputs: List[Any] = [], **kwargs):
        node_inputs = self.process_input(inputs)
        outputs = []

        for input in node_inputs:
            task = input.get("task", "")
            container = input.get("container") or get_current_container()
            log: List[str] = []

            if container is None:
                execution = {
                    "operation": self.node_name,
                    "task": task,
                    "output": "Error: no container available",
                    "format": "text",
                }
                outputs.append(execution)
                self.memory.add(self.id, execution)
                continue

            listing = self._list_py_files(container)
            files = await self._locate_files(task, container, listing)
            log.append(f"files selected: {files}")

            file_sections = []
            for path in files:
                content = self._read_file(container, path)
                if content is not None:
                    file_sections.append(self._render_file(path, content, task))
            files_text = "\n\n".join(file_sections)

            edit_prompt = (
                f"{task}\n\n"
                f"## Current file contents\n{files_text}\n\n"
                f"## Your task\n"
                "Fix the issue described above by editing the files shown.\n\n"
                f"{EDIT_FORMAT_INSTRUCTIONS}"
            )
            system = Message(
                role="system",
                content="You are an expert software engineer. You fix bugs by "
                        "producing precise search/replace edits against the "
                        "real file contents you are given.")

            edited: List[str] = []
            # Empty output is usually sampling variance (SEARCH text not copied
            # verbatim), so retry the whole edit round when nothing applied.
            for attempt in range(1, 3):
                prompt_text = edit_prompt if attempt == 1 else (
                    edit_prompt
                    + "\n\nNOTE: Your previous attempt produced no edits whose "
                      "SEARCH text matched the files. Copy SEARCH lines "
                      "character-for-character from the file contents above.")
                response = await self.llm.agen(
                    [system, Message(role="user", content=prompt_text)],
                    max_tokens=self.max_token,
                )
                blocks = parse_edit_blocks(str(response))
                log.append(f"attempt {attempt}: {len(blocks)} blocks")
                edited, failed = self._apply_blocks(container, blocks, files)
                log.append(f"attempt {attempt} applied to {edited}, {len(failed)} failed")

                if failed:
                    repair_sections = []
                    for path, search, replace in failed[:6]:
                        content = self._read_file(container, path) or ""
                        region = closest_region(content, search)
                        repair_sections.append(
                            f"### file: {path}\n"
                            f"Your SEARCH block did not match the file. "
                            f"You wrote:\n```\n{search}\n```\n"
                            f"The closest ACTUAL file content is:\n```python\n{region}\n```\n"
                            f"Intended replacement:\n```\n{replace}\n```")
                    repair_prompt = (
                        f"{task}\n\n"
                        "Some of your edits failed because the SEARCH text did not "
                        "match the actual file. For each failed edit below, re-emit "
                        "a corrected edit block whose SEARCH text is copied exactly "
                        "from the ACTUAL file content shown.\n\n"
                        + "\n\n".join(repair_sections) + "\n\n"
                        + EDIT_FORMAT_INSTRUCTIONS
                    )
                    response2 = await self.llm.agen(
                        [system, Message(role="user", content=repair_prompt)],
                        max_tokens=self.max_token,
                    )
                    blocks2 = parse_edit_blocks(str(response2))
                    edited2, failed2 = self._apply_blocks(container, blocks2, files)
                    log.append(f"attempt {attempt} repair: {len(blocks2)} blocks, "
                               f"applied to {edited2}, {len(failed2)} failed")
                    edited = list(dict.fromkeys(edited + edited2))

                if edited:
                    break

            reverted = self._syntax_check(container, edited)
            if reverted:
                log.append(f"reverted (syntax errors): {reverted}")

            output_text = "; ".join(log)
            execution = {
                "operation": self.node_name,
                "task": task,
                "container": container,
                "edited_files": edited,
                # passthrough for downstream nodes (TestFeedbackLoop needs them)
                "repo": input.get("repo", ""),
                "instance_id": input.get("instance_id", ""),
                "metadata": input.get("metadata", {}),
                "output": output_text,
                "format": "text",
            }
            outputs.append(execution)
            self.memory.add(self.id, execution)

        return outputs
