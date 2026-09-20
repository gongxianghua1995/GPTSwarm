"""
SWE-bench operations module.

Provides operations for SWE-bench tasks.
"""
from swarm.environment.operations.swe_bench.direct_answer import SWEDirectAnswer
from swarm.environment.operations.swe_bench.file_analyse import FileAnalyse
from swarm.environment.operations.swe_bench.web_search import SWEWebSearch
from swarm.environment.operations.swe_bench.file_ops import FileRead, FileWrite, BashCommand, GrepSearch, GitDiff
from swarm.environment.operations.swe_bench.code_edit import SearchReplaceEdit
from swarm.environment.operations.swe_bench.test_feedback import TestFeedbackLoop

__all__ = ['SWEDirectAnswer', 'FileAnalyse', 'SWEWebSearch', 'FileRead', 'FileWrite', 'BashCommand', 'GrepSearch', 'GitDiff', 'SearchReplaceEdit', 'TestFeedbackLoop']
