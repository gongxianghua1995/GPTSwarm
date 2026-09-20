"""
SWE-bench evaluator module.

Provides evaluation functionality for SWE-bench tasks using the official swebench package.
"""

import json
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Set
import tempfile
from dataclasses import dataclass

from swarm.environment.domain.swe_bench.env import SWEBenchEnv


@dataclass
class EvaluationResult:
    """Result of a single SWE-bench evaluation."""
    instance_id: str
    status: str  # FULL, EMPTY, ERROR, NO
    model_patch: str
    gold_patch: str
    error_message: Optional[str] = None


class SWEBenchEvaluator:
    """
    SWE-bench evaluator using official swebench harness.

    Supports:
    - Local evaluation with swebench package
    - Docker-based evaluation
    - Result parsing and aggregation
    """

    def __init__(
        self,
        env: SWEBenchEnv,
        output_dir: str = "./outputs/swebench",
        max_workers: int = 4,
        timeout: int = 900,
    ):
        self.env = env
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max_workers
        self.timeout = timeout

    def evaluate_predictions(
        self,
        predictions: List[Dict[str, Any]],
        dataset_name: str = "SWE-bench/SWE-bench_Verified",
        split: str = "test",
    ) -> Dict[str, Any]:
        """
        Evaluate predictions using official swebench harness.

        Args:
            predictions: List of prediction dicts with instance_id and model_patch
            dataset_name: Dataset name for swebench
            split: Dataset split

        Returns:
            Evaluation results dictionary
        """
        # Write predictions to temp file
        predictions_file = self.output_dir / "predictions.json"
        with open(predictions_file, 'w') as f:
            json.dump(predictions, f, indent=2)

        # Prepare report directory
        report_dir = self.output_dir / "reports"
        report_dir.mkdir(exist_ok=True)

        # Get instance IDs
        instance_ids = [p['instance_id'] for p in predictions]

        # Run evaluation
        try:
            # Try using swebench package directly
            result = self._run_swebench_evaluation(
                dataset_name=dataset_name,
                split=split,
                predictions_path=str(predictions_file),
                instance_ids=instance_ids,
                report_dir=str(report_dir),
            )
            return result

        except ImportError:
            print("swebench package not installed. Using manual evaluation.")
            return self._manual_evaluate(predictions)

    def _run_swebench_evaluation(
        self,
        dataset_name: str,
        split: str,
        predictions_path: str,
        instance_ids: List[str],
        report_dir: str,
    ) -> Dict[str, Any]:
        """Run evaluation using swebench package."""
        try:
            import swebench
        except ImportError:
            raise ImportError("Please install swebench: pip install swebench")

        # Run evaluation
        swebench.run_evaluation(
            dataset_name=dataset_name,
            split=split,
            instance_ids=instance_ids,
            predictions_path=predictions_path,
            max_workers=self.max_workers,
            timeout=self.timeout,
            report_dir=report_dir,
        )

        # Parse results
        model_name = "evaluation"
        run_id = "0"

        # Find summary file
        summary_files = list(Path(report_dir).glob(f"*.{run_id}.json"))
        if not summary_files:
            # Try alternative naming
            summary_files = list(Path(report_dir).glob("*.json"))

        if summary_files:
            summary_path = summary_files[0]
            summary = json.loads(summary_path.read_text())

            return {
                "resolved_ids": set(summary.get("resolved_ids", [])),
                "unresolved_ids": set(summary.get("unresolved_ids", [])),
                "empty_patch_ids": set(summary.get("empty_patch_ids", [])),
                "error_ids": set(summary.get("error_ids", [])),
                "total": len(instance_ids),
            }

        return {"error": "Could not find evaluation results"}

    def _manual_evaluate(
        self,
        predictions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Manual evaluation when swebench package is not available.

        This uses a simple diff-based evaluation.
        """
        resolved_ids: Set[str] = set()
        empty_patch_ids: Set[str] = set()
        error_ids: Set[str] = set()

        for pred in predictions:
            instance_id = pred['instance_id']
            model_patch = pred.get('model_patch', '')

            if not model_patch or model_patch.strip() == "":
                empty_patch_ids.add(instance_id)
            else:
                # For now, mark as unresolved without gold comparison
                # Full evaluation requires Docker harness
                error_ids.add(instance_id)

        return {
            "resolved_ids": resolved_ids,
            "unresolved_ids": error_ids,
            "empty_patch_ids": empty_patch_ids,
            "error_ids": error_ids,
            "total": len(predictions),
            "note": "Manual evaluation - install swebench for accurate results",
        }

    def evaluate_docker(
        self,
        predictions: List[Dict[str, Any]],
        docker_image: str,
        dataset_path: str,
    ) -> Dict[str, Any]:
        """
        Evaluate using Docker container.

        Based on EvoMAS benchmark_universal_guide.md Docker approach.

        Args:
            predictions: List of predictions
            docker_image: Docker image to use
            dataset_path: Path to SWE-bench dataset

        Returns:
            Evaluation results
        """
        # Write predictions to temp file
        predictions_file = self.output_dir / "predictions.json"
        with open(predictions_file, 'w') as f:
            json.dump(predictions, f)

        # Build docker command
        cmd = [
            "docker", "run",
            "--rm",
            "-v", f"{dataset_path}:/app/dataset",
            "-v", f"{self.output_dir}:/app/output",
            docker_image,
            "python", "-m", "swebench.harness.run_evaluation",
            "--predictions-path", "/app/output/predictions.json",
            "--output-path", "/app/output",
            "--max-workers", str(self.max_workers),
            "--timeout", str(self.timeout),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout * 2,  # Allow more time for Docker
            )

            if result.returncode == 0:
                # Parse output
                return self._parse_docker_output(result.stdout)
            else:
                return {"error": result.stderr}

        except subprocess.TimeoutExpired:
            return {"error": "Docker evaluation timed out"}
        except FileNotFoundError:
            return {"error": "Docker not found"}

    def _parse_docker_output(self, output: str) -> Dict[str, Any]:
        """Parse Docker evaluation output."""
        # Simple parsing - in practice, you'd parse the actual output format
        lines = output.strip().split('\n')

        results = {
            "resolved_ids": set(),
            "unresolved_ids": set(),
            "empty_patch_ids": set(),
            "error_ids": set(),
        }

        for line in lines:
            if ':' in line:
                parts = line.split(':', 1)
                key = parts[0].strip().lower()
                if key in results:
                    instance_id = parts[1].strip()
                    results[key].add(instance_id)

        return results

    def generate_report(
        self,
        results: Dict[str, Any],
        model_name: str = "baseline",
    ) -> Dict[str, Any]:
        """
        Generate evaluation report.

        Args:
            results: Evaluation results from evaluate_predictions
            model_name: Name of the model being evaluated

        Returns:
            Report dictionary with metrics
        """
        resolved_ids = results.get("resolved_ids", set())
        unresolved_ids = results.get("unresolved_ids", set())
        empty_patch_ids = results.get("empty_patch_ids", set())
        error_ids = results.get("error_ids", set())

        total = results.get("total", len(resolved_ids) + len(unresolved_ids))

        if total == 0:
            total = 1  # Avoid division by zero

        report = {
            "model_name": model_name,
            "total": total,
            "resolved": len(resolved_ids),
            "unresolved": len(unresolved_ids),
            "empty_patch": len(empty_patch_ids),
            "errors": len(error_ids),
            "resolved_ids": list(resolved_ids),
            "unresolved_ids": list(unresolved_ids),
            "empty_patch_ids": list(empty_patch_ids),
            "error_ids": list(error_ids),
            "resolution_rate": len(resolved_ids) / total,
        }

        # Save report
        report_file = self.output_dir / f"report_{model_name.replace('/', '__')}.json"
        with open(report_file, 'w') as f:
            json.dump(report, f, indent=2)

        return report
