import json
import pandas as pd
from typing import Union, List, Literal, Optional, Dict, Any
import numpy as np
from pathlib import Path

from experiments.evaluator.datasets.base_dataset import BaseDataset, SwarmInput


class SWEBenchDataset(BaseDataset):
    """
    SWE-bench dataset loader for GPTSwarm.

    Supports multiple SWE-bench variants:
    - swe-bench: princeton-nlp/SWE-bench
    - swe-bench-lite: princeton-nlp/SWE-bench_Lite
    - swe-bench-verified: princeton-nlp/SWE-bench_Verified
    """

    VARIANTS = {
        "swe-bench": "princeton-nlp/SWE-bench",
        "swe-bench-lite": "princeton-nlp/SWE-bench_Lite",
        "swe-bench-verified": "princeton-nlp/SWE-bench_Verified",
    }

    def __init__(
        self,
        variant: Literal["swe-bench", "swe-bench-lite", "swe-bench-verified"] = "swe-bench-verified",
        split: Union[Literal["train"], Literal["test"]] = "test",
        data_path: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> None:
        self._variant = variant
        self._split = split
        self._limit = limit

        # HuggingFace dataset will be downloaded on first access
        self._data_path = data_path
        self._instances: List[Dict[str, Any]] = []
        self._total_df: Optional[pd.DataFrame] = None
        self._load_data()

    @staticmethod
    def get_domain() -> str:
        return 'swe_bench'

    @property
    def split(self) -> str:
        return self._split

    def _load_data(self) -> None:
        """Load SWE-bench data from local path or HuggingFace."""
        if self._data_path and Path(self._data_path).exists():
            self._load_from_local(self._data_path)
        else:
            self._load_from_huggingface()

    def _load_from_local(self, data_path: str) -> None:
        """Load data from local JSON file or instance_id list."""
        with open(data_path, 'r') as f:
            data = json.load(f)

        # Check if this is a full instances file (list of instances with all fields)
        if isinstance(data, list) and len(data) > 0 and 'problem_statement' in data[0]:
            # Already full instance data
            self._instances = data
            self._total_df = pd.DataFrame(self._instances)
            if self._limit:
                self._total_df = self._total_df.head(self._limit)
                self._instances = self._instances[:self._limit]
            print(f"Loaded {len(self._instances)} full instances from {data_path}")
            return

        # Handle split file format: {"families": {"repo": {"test": [...]}}}
        if isinstance(data, dict) and 'families' in data:
            instance_ids = []
            for family_data in data.get('families', {}).values():
                for split_name in [self._split, 'test', 'train']:
                    if split_name in family_data:
                        instance_ids.extend(family_data[split_name])
                        break  # Only take the first matching split

            # Try to find corresponding full data file
            full_data_path = data_path.replace('_split.json', '_full.json')
            if Path(full_data_path).exists():
                # Load from full data file
                with open(full_data_path, 'r') as f:
                    full_data = json.load(f)

                if isinstance(full_data, list):
                    target_ids = set(instance_ids)
                    self._instances = [item for item in full_data if item.get('instance_id') in target_ids]
                    self._total_df = pd.DataFrame(self._instances)
                    if self._limit:
                        self._total_df = self._total_df.head(self._limit)
                        self._instances = self._instances[:self._limit]
                    print(f"Loaded {len(self._instances)} instances from {full_data_path}")
                    return

            # Fallback: try HuggingFace
            self._instances = self._load_full_instances(instance_ids)
            self._total_df = pd.DataFrame(self._instances)
            return

        # Handle single instance format
        if isinstance(data, dict) and 'instance_id' in data:
            self._instances = [data]
        # Handle list of instances
        elif isinstance(data, list):
            self._instances = data
        else:
            raise ValueError(f"Invalid data format in {data_path}")

        # Convert to DataFrame
        self._total_df = pd.DataFrame(self._instances)

        if self._limit:
            self._total_df = self._total_df.head(self._limit)
            self._instances = self._instances[:self._limit]

        print(f"Loaded {len(self._instances)} instances from {data_path}")

    def _load_full_instances(self, instance_ids: List[str]) -> List[Dict[str, Any]]:
        """Load full instance data for given instance_ids from HuggingFace."""
        try:
            from datasets import load_dataset
        except ImportError:
            raise ImportError(
                "Please install datasets package: pip install datasets"
            )

        hf_name = self.VARIANTS.get(self._variant, self._variant)
        print(f"Loading full instance data from {hf_name}...")

        # Load the full dataset
        dataset = load_dataset(hf_name, split='train')  # SWE-bench uses 'train' split

        # Filter by instance_ids
        instances = []
        instance_id_set = set(instance_ids)

        for item in dataset:
            if item['instance_id'] in instance_id_set:
                instances.append(dict(item))

        # Apply limit if specified
        if self._limit:
            instances = instances[:self._limit]

        print(f"Loaded {len(instances)} full instances")
        return instances

    def _load_from_huggingface(self) -> None:
        """Load data from HuggingFace."""
        try:
            from datasets import load_dataset
        except ImportError:
            raise ImportError(
                "Please install datasets package: pip install datasets"
            )

        hf_name = self.VARIANTS.get(self._variant, self._variant)
        print(f"Loading {hf_name} from HuggingFace...")

        dataset = load_dataset(hf_name, split=self._split)
        self._instances = [dict(item) for item in dataset]

        # Convert to DataFrame
        self._total_df = pd.DataFrame(self._instances)

        if self._limit:
            self._total_df = self._total_df.head(self._limit)
            self._instances = self._instances[:self._limit]

        print(f"Loaded {len(self._instances)} instances")

    def __len__(self) -> int:
        return len(self._total_df)

    def __getitem__(self, index: int) -> pd.DataFrame:
        record = self._total_df.iloc[index]
        assert isinstance(record, (pd.DataFrame, pd.Series))
        return record

    @staticmethod
    def record_to_swarm_input(record: pd.DataFrame) -> SwarmInput:
        """
        Convert SWE-bench record to swarm input format.

        SWE-bench fields:
        - instance_id: unique identifier
        - repo: repository name (e.g., "django/django")
        - base_commit: the commit hash to checkout to
        - patch: the gold patch to apply
        - problem_statement: description of the issue
        - FAIL_TO_PASS: list of failing test cases
        - PASS_TO_PASS: list of passing test cases (after fix)
        - environment_setup_commit: commit for environment setup
        - hints_text: optional hints
        - created_at: creation timestamp
        - test_patch: patch for test cases
        - repo_version: repository version
        - model_identities: model identities that solved this
        - license: repository license
        """
        # Extract problem statement and format as query
        problem_statement = record.get('problem_statement', '')
        repo = record.get('repo', '')

        # Format query with repository context
        query = f"""Repository: {repo}

## Problem Statement
{problem_statement}
"""

        # Include additional context if available
        if 'hints_text' in record and record['hints_text']:
            query += f"""
## Hints
{record['hints_text']}
"""

        if 'FAIL_TO_PASS' in record and record['FAIL_TO_PASS']:
            query += f"""
## Expected to Fail Tests
{', '.join(record['FAIL_TO_PASS'])}
"""

        input_dict = {
            "task": query,
            "instance_id": record.get('instance_id', ''),
            "repo": repo,
            "base_commit": record.get('base_commit', ''),
            "metadata": {
                "patch": record.get('patch', ''),
                "FAIL_TO_PASS": record.get('FAIL_TO_PASS', []),
                "PASS_TO_PASS": record.get('PASS_TO_PASS', []),
                "test_patch": record.get('test_patch', ''),
            }
        }

        return input_dict

    def postprocess_answer(self, answer: Union[str, List[str]]) -> str:
        """
        Postprocess the agent's answer to extract the patch.

        The answer should be a diff/patch string.
        """
        if isinstance(answer, list):
            if len(answer) > 0:
                answer = answer[0]
            else:
                answer = ""

        if not isinstance(answer, str):
            raise Exception(f"Expected string answer, got {type(answer)}")

        # Clean up the answer - extract patch if wrapped in markdown code blocks
        answer = answer.strip()

        # Remove markdown code block markers if present
        if answer.startswith("```diff"):
            answer = answer[7:]
        elif answer.startswith("```"):
            answer = answer[3:]

        if answer.endswith("```"):
            answer = answer[:-3]

        answer = answer.strip()

        # A unified diff must end with a newline: `strip()` above removes it,
        # and GNU patch rejects a patch whose last line is an add/delete line
        # without a trailing newline ("patch unexpectedly ends in middle of line").
        if answer.startswith("diff --git") or answer.startswith("---"):
            answer += "\n"

        return answer

    @staticmethod
    def record_to_target_answer(record: pd.DataFrame) -> str:
        """
        Get the ground truth patch for evaluation.
        """
        patch = record.get('patch', '')
        if not isinstance(patch, str):
            patch = ''
        return patch


class SWEBenchLiteDataset(SWEBenchDataset):
    """Alias for SWE-bench Lite."""

    def __init__(
        self,
        split: Union[Literal["train"], Literal["test"]] = "test",
        data_path: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> None:
        super().__init__(
            variant="swe-bench-lite",
            split=split,
            data_path=data_path,
            limit=limit,
        )


class SWEBenchVerifiedDataset(SWEBenchDataset):
    """Alias for SWE-bench Verified."""

    def __init__(
        self,
        split: Union[Literal["train"], Literal["test"]] = "test",
        data_path: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> None:
        super().__init__(
            variant="swe-bench-verified",
            split=split,
            data_path=data_path,
            limit=limit,
        )
