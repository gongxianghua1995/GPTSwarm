"""Fixed collaborative GPTSwarm baseline backed by installed mini-swe-agent."""
import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from swarm.environment.agents.swe_bench.mini_collaboration import build_fixed_swarm
from swarm.environment.agents.swe_bench.mini_runtime import MiniRuntime


ROOT = Path(__file__).resolve().parents[1]


async def generate(record, config, out, mini_python):
    runtime = MiniRuntime(record, config, out, mini_python)
    swarm = build_fixed_swarm(config['model'], runtime)
    graph = swarm.composite_graph
    manifest = dict(config, instance_id=record['instance_id'], base_commit=record['base_commit'],
                    edge_optimize=False, node_optimize=False, topology='fixed_role_dag_v1',
                    role_protocol='shared_task_budget_v4',
                    mini_python=mini_python, roles=swarm.agent_names,
                    nodes=[dict(id=n.id, name=n.node_name) for n in graph.nodes.values()],
                    edges=[(n.id, s.id) for n in graph.nodes.values() for s in n.successors],
                    inputs_policy='problem_statement_only; no hints, gold patches or evaluation test names')
    sources = [Path(__file__), *Path(ROOT / 'swarm/environment/agents/swe_bench').glob('mini_*.py'),
               ROOT / 'swarm/graph/graph.py', ROOT / 'swarm/graph/node.py', ROOT / 'swarm/graph/swarm.py']
    manifest['source_hashes'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    for source in sources:
        target = out / 'source_at_run' / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    (out / 'config.json').write_text(json.dumps(manifest, indent=2))
    status, error, prediction = 'completed', None, None
    try:
        await runtime.prepare()
        # Role time targets are advisory; the entire graph shares one deadline.
        await graph.run({'messages': []}, max_tries=1, max_time=config['task_seconds'] + 60)
        if any(not n.outputs for n in graph.nodes.values()):
            status = 'incomplete_graph'
    except asyncio.CancelledError:
        status, error = 'cancelled', 'CancelledError'
        raise
    except Exception as exc:
        status, error = 'execution_error', type(exc).__name__
    finally:
        try:
            actual = await runtime.export()
            (out / 'selected.patch').write_text(actual['patch'])
            prediction = dict(instance_id=record['instance_id'], model_patch=actual['patch'],
                              model_name_or_path=config['model'])
            (out / 'predictions.json').write_text(json.dumps([prediction], indent=2))
        except Exception as exc:
            status, error = 'patch_export_error', type(exc).__name__
        trace = [dict(id=n.id, name=n.node_name, outputs=n.outputs) for n in graph.nodes.values()]
        (out / 'trace.json').write_text(json.dumps(trace, ensure_ascii=False, indent=2))
        phases = [m for n in graph.nodes.values() for output in n.outputs
                  for m in output.get('messages', [])]
        phases = {m['phase']: m for m in phases}
        if status == 'completed' and any(m['exit_status'] not in {'Submitted', 'SkippedApproved'}
                                         for m in phases.values()):
            status = 'completed_with_role_failures'
        summary = dict(status=status, error=error, patch_chars=len(prediction['model_patch']) if prediction else None,
                       phases={k: dict(exit_status=v['exit_status'], verdict=v.get('verdict')) for k, v in phases.items()})
        (out / 'generation.json').write_text(json.dumps(summary, indent=2))
        await runtime.close()
    print(json.dumps(summary), flush=True)
    return prediction


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-path', default=str(ROOT / 'outputs/swebench/swebench_verified_test_154.json'))
    p.add_argument('--instance-id', default='django__django-10999')
    p.add_argument('--config', default=str(ROOT / 'config/swebench/mini_fixed.json'))
    p.add_argument('--mini-python', default=os.environ.get('MINISWE_PYTHON', sys.executable))
    p.add_argument('--output-dir')
    p.add_argument('--skip-eval', action='store_true')
    args = p.parse_args()
    load_dotenv(ROOT / '.env')
    config = json.loads(Path(args.config).read_text())
    if config['task_seconds'] <= 0 or any(v['seconds'] <= 0 or v['max_tokens'] <= 0 or v['steps'] <= 0
                                          for v in config['phases'].values()):
        raise ValueError('Budgets must be positive')
    records = json.loads(Path(args.data_path).read_text())
    record = next(r for r in records if r['instance_id'] == args.instance_id)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
    out = Path(args.output_dir) if args.output_dir else ROOT / 'outputs/swebench' / ('mini_fixed_' + stamp)
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    print(f'Artifacts: {out}', flush=True)
    prediction = asyncio.run(generate(record, config, out, args.mini_python))
    if not args.skip_eval and prediction:
        from experiments.swebench_mini_eval import evaluate
        evaluate(args.data_path, out, prediction)


if __name__ == '__main__':
    main()
