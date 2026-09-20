"""
SWE-bench benchmark runner for GPTSwarm.

This script runs SWE-bench evaluation using the GPTSwarm framework.
Supports multiple SWE-bench variants and evaluation modes.
"""

import asyncio
import argparse
import json
import os
import random
from pathlib import Path
from typing import Optional, Union, Literal, Dict, Any
from datetime import datetime

from swarm.graph.swarm import Swarm
from swarm.environment.domain.swe_bench.env import SWEBenchEnv
from swarm.environment.domain.swe_bench import env as swe_env
from swarm.environment.domain.swe_bench.evaluator import SWEBenchEvaluator
from swarm.environment.domain.swe_bench.parser import PatchParser
from experiments.evaluator.datasets.swe_bench_dataset import SWEBenchDataset


def parse_args():
    parser = argparse.ArgumentParser(description="Run SWE-bench benchmark with GPTSwarm.")

    parser.add_argument(
        '--mode',
        type=str,
        default='DirectAnswer',
        choices=['DirectAnswer', 'FullConnectedSwarm', 'RandomSwarm', 'OptimizedSwarm'],
        help="Evaluation mode"
    )

    parser.add_argument(
        '--variant',
        type=str,
        default='swe-bench-verified',
        choices=['swe-bench', 'swe-bench-lite', 'swe-bench-verified'],
        help="SWE-bench variant to use"
    )

    parser.add_argument(
        '--split',
        type=str,
        default='test',
        choices=['train', 'test'],
        help="Dataset split to evaluate on"
    )

    parser.add_argument(
        '--limit',
        type=int,
        default=None,
        help="Limit number of instances to evaluate (for debugging)"
    )

    parser.add_argument(
        '--data-path',
        type=str,
        default=None,
        help="Path to local SWE-bench data JSON file (optional)"
    )

    parser.add_argument(
        '--model-name',
        type=str,
        default=None,
        help="LLM model name (default: ChatGPT4)"
    )

    parser.add_argument(
        '--output-dir',
        type=str,
        default='./outputs/swebench',
        help="Output directory for results"
    )

    parser.add_argument(
        '--repo-cache-dir',
        type=str,
        default='./repos',
        help="Directory to cache cloned repositories"
    )

    parser.add_argument(
        '--max-workers',
        type=int,
        default=4,
        help="Maximum number of parallel workers"
    )

    parser.add_argument(
        '--num-truthful-agents',
        type=int,
        default=1,
        help="Number of truthful agents for swarm mode"
    )

    parser.add_argument(
        '--num-iterations',
        type=int,
        default=200,
        help="Number of optimization iterations for OptimizedSwarm mode"
    )

    parser.add_argument(
        '--agent-type',
        type=str,
        default='SWECodeAgent',
        choices=['SWECodeAgent', 'SWECodeIO', 'SWEReActCodeAgent', 'SWEMultiStepAgent', 'SWEEditAgent'],
        help="Type of SWE-bench agent to use"
    )

    parser.add_argument(
        '--debug',
        action='store_true',
        default=False,
        help="Enable debug mode with limited instances"
    )

    return parser.parse_args()


class SWEBenchRunner:
    """
    Runner class for SWE-bench benchmark evaluation.
    """

    def __init__(
        self,
        variant: str = 'swe-bench-verified',
        split: str = 'test',
        model_name: Optional[str] = None,
        output_dir: str = './outputs/swebench',
        repo_cache_dir: str = './repos',
        max_workers: int = 4,
        debug: bool = False,
    ):
        self.variant = variant
        self.split = split
        self.model_name = model_name
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.debug = debug
        self.max_workers = max_workers

        # Initialize environment
        self.env = SWEBenchEnv(
            repo_cache_dir=repo_cache_dir,
            max_workers=max_workers,
        )

        # Initialize evaluator
        self.evaluator = SWEBenchEvaluator(
            env=self.env,
            output_dir=str(self.output_dir),
            max_workers=max_workers,
        )

        # Results storage
        self.predictions: Dict[str, Dict[str, Any]] = {}

    def _group_tasks_by_repo(
        self,
        dataset: SWEBenchDataset,
    ) -> Dict[str, list]:
        """
        Group tasks by repository for optimal parallelization.

        Based on EvoMAS benchmark_universal_guide.md experience:
        - Same repo tasks should be assigned to same worker
        - Prevents concurrent repo access conflicts
        """
        repo_tasks: Dict[str, list] = {}

        for i, record in enumerate(dataset):
            repo = record.get('repo', '')
            if repo not in repo_tasks:
                repo_tasks[repo] = []
            repo_tasks[repo].append(i)

        return repo_tasks

    async def run_direct_answer(
        self,
        dataset: SWEBenchDataset,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Run direct answer (baseline) evaluation.
        """
        from swarm.environment.agents.swe_bench.code_io import SWECodeIOAgent

        print(f"Running DirectAnswer evaluation on {len(dataset)} instances...")

        agent = SWECodeIOAgent(domain='swe_bench', model_name=self.model_name)

        results = []
        for i, record in enumerate(dataset):
            if limit and i >= limit:
                break

            print(f"\n{'='*80}")
            print(f"Processing instance {i+1}/{min(len(dataset), limit) if limit else len(dataset)}")
            print(f"Instance ID: {record.get('instance_id', 'unknown')}")
            print(f"Repo: {record.get('repo', 'unknown')}")

            # Get swarm input
            input_dict = dataset.record_to_swarm_input(record)
            instance_id = input_dict.get('instance_id', f'instance_{i}')

            try:
                # Run agent
                answer = await agent.run(input_dict)

                # Postprocess answer
                patch = dataset.postprocess_answer(answer)

                # Store prediction
                self.predictions[instance_id] = {
                    'instance_id': instance_id,
                    'model_patch': patch,
                }

                results.append({
                    'instance_id': instance_id,
                    'status': 'completed',
                    'patch_length': len(patch),
                })

                print(f"Generated patch ({len(patch)} chars)")

            except Exception as e:
                print(f"Error processing instance: {e}")
                results.append({
                    'instance_id': instance_id,
                    'status': 'error',
                    'error': str(e),
                })

        return {
            'mode': 'DirectAnswer',
            'total': len(results),
            'completed': sum(1 for r in results if r['status'] == 'completed'),
            'errors': sum(1 for r in results if r['status'] == 'error'),
            'results': results,
        }

    async def run_swarm(
        self,
        dataset: SWEBenchDataset,
        mode: str,
        limit: Optional[int] = None,
        num_truthful_agents: int = 1,
        agent_type: str = "SWECodeAgent",
    ) -> Dict[str, Any]:
        """
        Run swarm-based evaluation with code analysis capabilities.

        Args:
            dataset: SWE-bench dataset
            mode: Swarm mode (FullConnectedSwarm, OptimizedSwarm, etc.)
            limit: Limit number of instances
            num_truthful_agents: Number of truthful agents
            agent_type: Type of agent to use ("SWECodeAgent", "SWECodeIOAgent", etc.)
        """
        from swarm.environment.operations.final_decision import MergingStrategy

        print(f"Running {mode} evaluation with {num_truthful_agents} truthful agents...")
        print(f"Agent type: {agent_type}")

        # Build agent list based on mode
        if mode == "FullConnectedSwarm":
            agent_name_list = ["SWECodeAgent", "SWEReActCodeAgent", "SWEMultiStepAgent"]
        elif mode == "RandomSwarm":
            # Random selection of agents
            agent_types = ["SWECodeAgent", "SWECodeIO", "SWEReActCodeAgent"]
            agent_name_list = [random.choice(agent_types) for _ in range(num_truthful_agents + 1)]
        else:
            # OptimizedSwarm or default
            agent_name_list = [agent_type] * (num_truthful_agents + 1)

        print(f"Agent list: {agent_name_list}")

        # Collect records (respecting limit)
        records = []
        for i, record in enumerate(dataset):
            if limit and i >= limit:
                break
            records.append(record)

        # Group by repo: same-repo cases run sequentially in one worker,
        # different repos run in parallel (bounded by max_workers).
        repo_groups: Dict[str, list] = {}
        for record in records:
            repo_groups.setdefault(record.get('repo', ''), []).append(record)

        print(f"Total {len(records)} instances in {len(repo_groups)} repo groups: "
              f"{ {r: len(v) for r, v in repo_groups.items()} }")

        results = []
        sem = asyncio.Semaphore(self.max_workers)
        partial_file = self.output_dir / "predictions_partial.json"

        def make_swarm():
            return Swarm(
                agent_name_list,
                'swe_bench',
                model_name=self.model_name,
                final_node_class="FinalDecision",
                final_node_kwargs=dict(strategy=MergingStrategy.MajorityVote),
                edge_optimize=(mode == 'OptimizedSwarm'),
            )

        async def process_repo(repo: str, repo_records: list):
            async with sem:
                for record in repo_records:
                    input_dict = dataset.record_to_swarm_input(record)
                    instance_id = input_dict.get('instance_id', 'unknown')
                    print(f"[{repo}] Processing {instance_id}", flush=True)
                    container = None
                    try:
                        # Start per-case container (image: swebench/sweb.eval.x86_64.*)
                        container = swe_env.start_container(instance_id)
                        input_dict['container'] = container
                        swarm = make_swarm()
                        answer = await swarm.arun(input_dict)
                        patch = dataset.postprocess_answer(answer)
                        self.predictions[instance_id] = {
                            'instance_id': instance_id,
                            'model_patch': patch,
                        }
                        results.append({
                            'instance_id': instance_id,
                            'status': 'completed',
                            'patch_length': len(patch),
                        })
                        print(f"[{repo}] {instance_id}: patch {len(patch)} chars "
                              f"({len(results)}/{len(records)})", flush=True)
                    except Exception as e:
                        print(f"[{repo}] {instance_id} Error: {e}", flush=True)
                        results.append({
                            'instance_id': instance_id,
                            'status': 'error',
                            'error': str(e),
                        })
                    finally:
                        if container:
                            swe_env.stop_container(container)
                    # Incremental save after each case
                    try:
                        with open(partial_file, 'w') as f:
                            json.dump(list(self.predictions.values()), f, indent=2)
                    except Exception:
                        pass

        await asyncio.gather(*(process_repo(r, recs)
                               for r, recs in repo_groups.items()))

        return {
            'mode': mode,
            'total': len(results),
            'completed': sum(1 for r in results if r['status'] == 'completed'),
            'errors': sum(1 for r in results if r['status'] == 'error'),
            'results': results,
        }

    def save_predictions(self) -> str:
        """Save predictions to file."""
        predictions_list = list(self.predictions.values())

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"predictions_{timestamp}.json"
        filepath = self.output_dir / filename

        with open(filepath, 'w') as f:
            json.dump(predictions_list, f, indent=2)

        print(f"Predictions saved to {filepath}")
        return str(filepath)

    def generate_report(self) -> Dict[str, Any]:
        """Generate evaluation report."""
        predictions_list = list(self.predictions.values())

        # Clean patches
        for pred in predictions_list:
            pred['model_patch'] = PatchParser.clean_patch(pred.get('model_patch', ''))

        # Save cleaned predictions
        cleaned_file = self.output_dir / "predictions_cleaned.json"
        with open(cleaned_file, 'w') as f:
            json.dump(predictions_list, f, indent=2)

        # Evaluate
        results = self.evaluator.evaluate_predictions(predictions_list)

        # Generate report
        report = self.evaluator.generate_report(
            results,
            model_name=self.model_name or "baseline"
        )

        return report


async def main():
    args = parse_args()

    print("="*80)
    print("SWE-bench Benchmark Runner for GPTSwarm")
    print("="*80)
    print(f"Variant: {args.variant}")
    print(f"Split: {args.split}")
    print(f"Mode: {args.mode}")
    print(f"Model: {args.model_name or 'default (ChatGPT4)'}")
    print("="*80)

    # Create runner
    runner = SWEBenchRunner(
        variant=args.variant,
        split=args.split,
        model_name=args.model_name,
        output_dir=args.output_dir,
        repo_cache_dir=args.repo_cache_dir,
        max_workers=args.max_workers,
        debug=args.debug,
    )

    # Load dataset
    limit = 5 if args.debug else args.limit
    dataset = SWEBenchDataset(
        variant=args.variant,
        split=args.split,
        data_path=args.data_path,
        limit=limit,
    )

    print(f"Loaded {len(dataset)} instances from {args.variant}")

    # Run evaluation
    if args.mode == 'DirectAnswer':
        eval_results = await runner.run_direct_answer(dataset, limit=limit)
    else:
        eval_results = await runner.run_swarm(
            dataset,
            mode=args.mode,
            limit=limit,
            num_truthful_agents=args.num_truthful_agents,
            agent_type=args.agent_type,
        )

    # Save predictions
    runner.save_predictions()

    # Generate report
    print("\n" + "="*80)
    print("Generating evaluation report...")
    print("="*80)

    report = runner.generate_report()

    print("\n" + "="*80)
    print("EVALUATION SUMMARY")
    print("="*80)
    print(f"Total instances: {report['total']}")
    print(f"Resolved: {report['resolved']}")
    print(f"Unresolved: {report['unresolved']}")
    print(f"Empty patches: {report['empty_patch']}")
    print(f"Errors: {report['errors']}")
    print(f"Resolution rate: {report['resolution_rate']:.2%}")
    print("="*80)

    # Save summary
    summary_file = Path(args.output_dir) / "evaluation_summary.json"
    with open(summary_file, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"\nSummary saved to {summary_file}")

    return report


if __name__ == "__main__":
    asyncio.run(main())
