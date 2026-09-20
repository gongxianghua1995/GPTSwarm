#!/usr/bin/env python
"""Full test-split run with the GPTSwarm-native SWERepairAgent
(SearchReplaceEdit -> TestFeedbackLoop -> GitDiff), network-isolated
per-case containers. Resumable: already-present non-empty predictions
are skipped."""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Dict

from swarm.environment.agents.swe_bench import SWERepairAgent
from swarm.environment.domain.swe_bench import env as swe_env
from experiments.evaluator.datasets.swe_bench_dataset import SWEBenchDataset


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--data-path', default='outputs/swebench/swebench_verified_test_154.json')
    p.add_argument('--model-name', default='DeepSeek-V4-Flash-0731')
    p.add_argument('--output', default='outputs/swebench/predictions_repair.json')
    p.add_argument('--max-workers', type=int, default=6)
    p.add_argument('--limit', type=int, default=None)
    p.add_argument('--instance-ids', nargs='*', default=None,
                   help='optional subset of instance ids')
    return p.parse_args()


async def main():
    args = parse_args()

    dataset = SWEBenchDataset(variant='swe-bench-verified', split='test',
                              data_path=args.data_path)
    records = [dataset[i] for i in range(len(dataset))]
    if args.instance_ids:
        wanted = set(args.instance_ids)
        records = [r for r in records if r.get('instance_id') in wanted]
    if args.limit:
        records = records[:args.limit]
    print(f"Running SWERepairAgent on {len(records)} instances "
          f"(network-isolated containers)", flush=True)

    predictions: Dict[str, dict] = {}
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    trace_dir = out_path.parent / (out_path.stem + '_traces')
    trace_dir.mkdir(exist_ok=True)
    if out_path.exists():
        try:
            for p in json.load(open(out_path)):
                predictions[p['instance_id']] = p
            n_done = sum(1 for p in predictions.values() if p.get('model_patch'))
            print(f"Resuming: {n_done} non-empty predictions already present")
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f'Cannot resume invalid predictions: {out_path}') from exc

    sem = asyncio.Semaphore(args.max_workers)
    done = [0]
    lock = asyncio.Lock()

    async def process(record):
        async with sem:
            input_dict = dataset.record_to_swarm_input(record)
            iid = input_dict.get('instance_id', 'unknown')
            if predictions.get(iid, {}).get('model_patch'):
                done[0] += 1
                return
            container = None
            try:
                container = swe_env.start_container(iid)  # --network none
                input_dict['container'] = container
                agent = SWERepairAgent(domain='swe_bench',
                                       model_name=args.model_name)
                answer = await agent.run(input_dict, max_time=2400)
                trace = {node.node_name: node.outputs for node in agent.nodes.values()}
                (trace_dir / f'{iid}.json').write_text(json.dumps(trace, indent=2, default=str))
                patch = dataset.postprocess_answer(answer)
                if not patch.startswith('diff --git'):
                    patch = ''
                predictions[iid] = {'instance_id': iid, 'model_patch': patch,
                                    'model_name_or_path': args.model_name}
                done[0] += 1
                print(f"{iid}: patch {len(patch)} chars ({done[0]}/{len(records)})",
                      flush=True)
            except Exception as e:
                done[0] += 1
                print(f"{iid} Error: {e}", flush=True)
                predictions[iid] = {'instance_id': iid, 'model_patch': '',
                                    'model_name_or_path': args.model_name}
            finally:
                if container:
                    swe_env.stop_container(container)
            async with lock:
                temp_path = out_path.with_suffix('.json.tmp')
                with open(temp_path, 'w') as f:
                    json.dump(list(predictions.values()), f, indent=2)
                temp_path.replace(out_path)

    await asyncio.gather(*(process(r) for r in records))

    n_nonempty = sum(1 for p in predictions.values() if p['model_patch'].strip())
    print(f"\nDone. {n_nonempty}/{len(predictions)} non-empty patches -> {out_path}")


if __name__ == '__main__':
    asyncio.run(main())
