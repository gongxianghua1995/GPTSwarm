"""
SWE-bench agent module.

Provides agents specialized for software engineering tasks.
"""
from swarm.environment.agents.swe_bench.code_io import SWECodeIOAgent
from swarm.environment.agents.swe_bench.code_react import SWEReActAgent
from .code_agent import SWECodeAgent, SWEReActCodeAgent, SWEMultiStepAgent, SWEEditAgent, SWERepairAgent
from .native_agent import SWECapableAgent

__all__ = [
    'SWECodeIOAgent',
    'SWEReActAgent',
    'SWECodeAgent',
    'SWEReActCodeAgent',
    'SWEMultiStepAgent',
    'SWEEditAgent',
    'SWERepairAgent',
    'SWECapableAgent',
]
