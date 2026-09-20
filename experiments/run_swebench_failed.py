#!/usr/bin/env python
"""Regenerate patches for the instances that still errored after eval_002,
using the SWEEditAgent (real file reads + search/replace edits + git diff)."""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Dict

from swarm.environment.agents.swe_bench import SWEEditAgent
from swarm.environment.domain.swe_bench import env as swe_env
from experiments.evaluator.datasets.swe_bench_dataset import SWEBenchDataset


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--report', default='DeepSeek-V4-Flash-0731.eval_002.json',
                   help='eval report whose error_ids will be rerun')
    p.add_argument('--data-path', default='datasets/swebench/verified_new_full.json')
    p.add_argument('--model-name', default='DeepSeek-V4-Flash-0731')
    p.add_argument('--output', default='outputs/swebench/predictions_retry.json')
    p.add_argument('--max-workers', type=int, default=4)
    p.add_argument('--limit', type=int, default=None)
    return p.parse_args()


async def main():
    args = parse_args()

    with open(args.report) as f:
        error_ids = set(json.load(f)['error_ids'])
    print(f"Rerunning {len(error_ids)} failed instances with SWEEditAgent")

    dataset = SWEBenchDataset(variant='swe-bench-verified', split='test',
                              data_path=args.data_path)
    records = [dataset[i] for i in range(len(dataset))
               if dataset[i].get('instance_id') in error_ids]
    if args.limit:
        records = records[:args.limit]
    print(f"Matched {len(records)} records in dataset")

    repo_groups: Dict[str, list] = {}
    for r in records:
        repo_groups.setdefault(r.get('repo', ''), []).append(r)
    print({k: len(v) for k, v in repo_groups.items()})

    predictions: Dict[str, dict] = {}
    out_path = Path(args.output)
    if out_path.exists():
        try:
            for p in json.load(open(out_path)):
                predictions[p['instance_id']] = p
            print(f"Resuming: {len(predictions)} predictions already present")
        except Exception:
            pass

    sem = asyncio.Semaphore(args.max_workers)
    done = [0]

    async def process_repo(repo, repo_records):
        async with sem:
            for record in repo_records:
                input_dict = dataset.record_to_swarm_input(record)
                iid = input_dict.get('instance_id', 'unknown')
                if iid in predictions and predictions[iid].get('model_patch'):
                    done[0] += 1
                    continue
                container = None
                try:
                    container = swe_env.start_container(iid)
                    input_dict['container'] = container
                    agent = SWEEditAgent(domain='swe_bench',
                                         model_name=args.model_name)
                    answer = await agent.run(input_dict)
                    patch = dataset.postprocess_answer(answer)
                    predictions[iid] = {'instance_id': iid, 'model_patch': patch,
                                        'model_name_or_path': args.model_name}
                    done[0] += 1
                    print(f"[{repo}] {iid}: patch {len(patch)} chars "
                          f"({done[0]}/{len(records)})", flush=True)
                except Exception as e:
                    done[0] += 1
                    print(f"[{repo}] {iid} Error: {e}", flush=True)
                    predictions[iid] = {'instance_id': iid, 'model_patch': '',
                                        'model_name_or_path': args.model_name}
                finally:
                    if container:
                        swe_env.stop_container(container)
                with open(out_path, 'w') as f:
                    json.dump(list(predictions.values()), f, indent=2)

    await asyncio.gather(*(process_repo(r, recs)
                           for r, recs in repo_groups.items()))

    n_nonempty = sum(1 for p in predictions.values() if p['model_patch'].strip())
    print(f"\nDone. {n_nonempty}/{len(predictions)} non-empty patches -> {out_path}")


if __name__ == '__main__':
    asyncio.run(main())
