"""
Patch parser for SWE-bench.

Provides utilities for parsing, validating, and manipulating patches.
"""

import re
from typing import Optional, List, Tuple
from dataclasses import dataclass


@dataclass
class PatchFile:
    """Represents a file modified by a patch."""
    path: str
    hunks: List['PatchHunk']


@dataclass
class PatchHunk:
    """Represents a single hunk in a patch."""
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    lines: List[str]


class PatchParser:
    """
    Parser for unified diff patches.

    Handles:
    - Parsing patch format
    - Extracting file changes
    - Validating patch syntax
    """

    # Regex patterns for patch parsing
    PATCH_HEADER_PATTERN = re.compile(r'^diff --git a/(.*) b/(.*)$')
    OLD_FILE_PATTERN = re.compile(r'^--- (?:a/)?(.*)$')
    NEW_FILE_PATTERN = re.compile(r'^\+\+\+ (?:b/)?(.*)$')
    HUNK_HEADER_PATTERN = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$')

    @classmethod
    def parse_patch(cls, patch_content: str) -> List[PatchFile]:
        """
        Parse a unified diff patch into structured format.

        Args:
            patch_content: The patch/diff content as string

        Returns:
            List of PatchFile objects representing file changes
        """
        if not patch_content or not patch_content.strip():
            return []

        files = []
        current_file: Optional[PatchFile] = None
        current_hunk_lines: List[str] = []
        current_hunk_start: Optional[int] = None
        current_hunk_old_start: Optional[int] = None
        current_hunk_old_lines: Optional[int] = None
        current_hunk_new_start: Optional[int] = None
        current_hunk_new_lines: Optional[int] = None
        in_hunk = False

        lines = patch_content.split('\n')
        i = 0

        while i < len(lines):
            line = lines[i]

            # New file diff
            diff_match = cls.PATCH_HEADER_PATTERN.match(line)
            if diff_match:
                # Save previous file if exists
                if current_file is not None and current_hunk_start is not None:
                    hunk = cls._create_hunk(
                        current_hunk_old_start,
                        current_hunk_old_lines,
                        current_hunk_new_start,
                        current_hunk_new_lines,
                        current_hunk_lines,
                    )
                    if hunk:
                        current_file.hunks.append(hunk)
                    current_hunk_lines = []
                    current_hunk_start = None

                file_path = diff_match.group(1)
                current_file = PatchFile(path=file_path, hunks=[])
                files.append(current_file)
                in_hunk = False
                i += 1
                continue

            # Old file marker
            old_match = cls.OLD_FILE_PATTERN.match(line)
            if old_match:
                in_hunk = False
                i += 1
                continue

            # New file marker
            new_match = cls.NEW_FILE_PATTERN.match(line)
            if new_match:
                in_hunk = False
                i += 1
                continue

            # Hunk header
            hunk_match = cls.HUNK_HEADER_PATTERN.match(line)
            if hunk_match:
                # Save previous hunk
                if current_file is not None and current_hunk_start is not None:
                    hunk = cls._create_hunk(
                        current_hunk_old_start,
                        current_hunk_old_lines,
                        current_hunk_new_start,
                        current_hunk_new_lines,
                        current_hunk_lines,
                    )
                    if hunk:
                        current_file.hunks.append(hunk)

                # Start new hunk
                current_hunk_old_start = int(hunk_match.group(1))
                current_hunk_old_lines = int(hunk_match.group(2) or 1)
                current_hunk_new_start = int(hunk_match.group(3))
                current_hunk_new_lines = int(hunk_match.group(4) or 1)
                current_hunk_lines = []
                current_hunk_start = i
                in_hunk = True
                i += 1
                continue

            # Hunk content lines
            if in_hunk and current_file is not None:
                if line.startswith('---') or line.startswith('+++') or line.startswith('diff'):
                    # End of hunk, start of next file
                    hunk = cls._create_hunk(
                        current_hunk_old_start,
                        current_hunk_old_lines,
                        current_hunk_new_start,
                        current_hunk_new_lines,
                        current_hunk_lines,
                    )
                    if hunk:
                        current_file.hunks.append(hunk)
                    current_hunk_lines = []
                    current_hunk_start = None
                    in_hunk = False
                    continue
                else:
                    current_hunk_lines.append(line)

            i += 1

        # Save last hunk
        if current_file is not None and current_hunk_start is not None:
            hunk = cls._create_hunk(
                current_hunk_old_start,
                current_hunk_old_lines,
                current_hunk_new_start,
                current_hunk_new_lines,
                current_hunk_lines,
            )
            if hunk:
                current_file.hunks.append(hunk)

        return files

    @classmethod
    def _create_hunk(
        cls,
        old_start: Optional[int],
        old_lines: Optional[int],
        new_start: Optional[int],
        new_lines: Optional[int],
        lines: List[str],
    ) -> Optional[PatchHunk]:
        """Create a PatchHunk from parsed data."""
        if old_start is None or old_lines is None:
            return None
        return PatchHunk(
            old_start=old_start,
            old_lines=old_lines,
            new_start=new_start or old_start,
            new_lines=new_lines or len(lines),
            lines=lines,
        )

    @classmethod
    def extract_changed_files(cls, patch_content: str) -> List[str]:
        """
        Extract list of files changed by a patch.

        Args:
            patch_content: The patch content

        Returns:
            List of file paths that would be modified
        """
        files = cls.parse_patch(patch_content)
        return [f.path for f in files]

    @classmethod
    def validate_patch_syntax(cls, patch_content: str) -> Tuple[bool, Optional[str]]:
        """
        Validate that a patch has valid unified diff syntax.

        Args:
            patch_content: The patch to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not patch_content or not patch_content.strip():
            return False, "Empty patch"

        lines = patch_content.split('\n')

        has_diff_header = False
        has_valid_hunks = False

        for line in lines:
            if cls.PATCH_HEADER_PATTERN.match(line):
                has_diff_header = True
            elif cls.HUNK_HEADER_PATTERN.match(line):
                has_valid_hunks = True

        if not has_diff_header:
            return False, "No diff header found"

        if not has_valid_hunks:
            return False, "No valid hunks found"

        return True, None

    @classmethod
    def clean_patch(cls, patch_content: str) -> str:
        """
        Clean and normalize a patch string.

        Removes common artifacts from LLM output.
        """
        if not patch_content:
            return ""

        # Remove markdown code blocks
        if patch_content.startswith("```diff"):
            patch_content = patch_content[7:]
        elif patch_content.startswith("```"):
            patch_content = patch_content[3:]

        if patch_content.endswith("```"):
            patch_content = patch_content[:-3]

        # Remove common prefixes
        lines = patch_content.split('\n')
        cleaned_lines = []

        for line in lines:
            # Skip non-diff lines that might be artifacts
            if line.startswith("Here's") or line.startswith("This patch"):
                continue
            if line.startswith("```"):
                continue
            cleaned_lines.append(line)

        # Rejoin and strip
        result = '\n'.join(cleaned_lines).strip()

        # Ensure patch starts with diff header if it looks like a valid patch
        if 'diff --git' in result and not result.startswith('diff'):
            # Find the first diff and extract from there
            idx = result.find('diff --git')
            result = result[idx:]

        return result
