"""
SWE-bench domain module for GPTSwarm.

This module provides SWE-bench specific evaluation and environment handling.
"""
from swarm.environment.domain.swe_bench.evaluator import SWEBenchEvaluator
from swarm.environment.domain.swe_bench.env import SWEBenchEnv

__all__ = ['SWEBenchEvaluator', 'SWEBenchEnv']
