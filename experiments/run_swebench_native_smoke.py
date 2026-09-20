"""One-case native, unoptimized GPTSwarm baseline with graph traces and harness evaluation."""
import argparse
import asyncio
import json
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from experiments.evaluator.datasets.swe_bench_dataset import SWEBenchDataset
from swarm.environment.domain.swe_bench import env
from swarm.environment.agents.swe_bench import SWECapableAgent
from swarm.graph.swarm import Swarm


def build_swarm(model_name, agents=3, seed=0):
    if agents < 1:
        raise ValueError('agents must be positive')
    swarm = Swarm(['SWECapableAgent'] * agents, 'swe_bench', model_name=model_name,
                  edge_optimize=False, node_optimize=False,
                  final_node_class='NativePatchVote', final_node_kwargs={'seed': seed})
    for slot, agent in enumerate(swarm.used_agents):
        agent.input_nodes[0].slot = slot
    return swarm


async def generate(args, out):
    dataset = SWEBenchDataset(data_path='outputs/swebench/swebench_verified_test_154.json')
    record = next((dataset[i] for i in range(len(dataset))
                   if dataset[i]['instance_id'] == args.instance_id), None)
    if record is None:
        raise ValueError(f'Instance not in test split: {args.instance_id}')
    random.seed(args.seed)
    swarm = build_swarm(args.model_name, args.agents, args.seed)
    names = swarm.agent_names
    # With no candidate edges, this is the native execution graph. Run it
    # directly to retain actual node outputs, which arun's deepcopy hides.
    graph = swarm.composite_graph
    owners = {node.id: f'agent_{slot}' for slot, agent in enumerate(swarm.used_agents)
              for node in agent.nodes.values()}
    edges = [(node.id, successor.id) for node in graph.nodes.values()
             for successor in node.successors]
    assert not swarm.potential_connections
    assert len(graph.decision_method.predecessors) == args.agents
    assert all(dst == graph.decision_method.id or owners[src] == owners[dst]
               for src, dst in edges)
    config = dict(instance_id=args.instance_id, model=args.model_name,
                  agents=names, edge_optimize=False, node_optimize=False,
                  rounds=1, max_node_tries=1, node_timeout=2400,
                  strategy='NativePatchVote', seed=args.seed,
                  shared_container=False, network='none',
                  agent_limits={'max_files': 3, 'file_chars': 30000,
                                'edit_attempts': 2, 'verification_rounds': 3,
                                'max_regression_tests': 5, 'temperature': 0.2},
                  nodes=[{'id': n.id, 'name': n.node_name,
                          'agent': owners.get(n.id, 'final')} for n in graph.nodes.values()],
                  edges=edges)
    (out / 'config.json').write_text(json.dumps(config, indent=2))
    inputs = dataset.record_to_swarm_input(record)
    # Gold source/test patches are evaluation-only, never agent inputs or traces.
    inputs['metadata'].pop('patch', None)
    inputs['metadata'].pop('test_patch', None)
    containers = []
    start = time.monotonic()
    try:
        for slot in range(args.agents):
            name = f'gptswarm_native_{uuid.uuid4().hex[:12]}_a{slot}'
            containers.append(env.start_container(args.instance_id, name=name))
        inputs['containers'] = containers
        config['containers'] = containers
        (out / 'config.json').write_text(json.dumps(config, indent=2))
        print(f'Running {args.instance_id}: {names}, native fixed graph, one pass', flush=True)
        answer = await graph.run(inputs, max_tries=1, max_time=2400)
        missing = [f'{owners.get(n.id, "final")}:{n.node_name}'
                   for n in graph.nodes.values() if not n.outputs]
        if missing:
            raise RuntimeError(f'Nodes failed to return outputs: {missing}')
        candidates = [agent.output_nodes[0].outputs[0] for agent in swarm.used_agents]
        (out / 'candidates.json').write_text(json.dumps(candidates, indent=2))
        for candidate in candidates:
            print(f'Agent {candidate["agent_index"]}: valid={candidate["valid"]}, '
                  f'patch={len(candidate["output"])} chars, files={candidate["changed_files"]}', flush=True)
        patch = dataset.postprocess_answer(answer)
        predictions = [{'instance_id': args.instance_id, 'model_patch': patch,
                        'model_name_or_path': args.model_name}]
        (out / 'predictions.json').write_text(json.dumps(predictions, indent=2))
        (out / 'selected.patch').write_text(patch)
        print(f'Selected patch: {len(patch)} chars', flush=True)
    finally:
        trace = [{'id': n.id, 'name': n.node_name, 'agent': owners.get(n.id, 'final'),
                  'outputs': n.outputs} for n in graph.nodes.values()]
        (out / 'trace.json').write_text(json.dumps(trace, indent=2, default=str))
        config['generation_seconds'] = time.monotonic() - start
        (out / 'config.json').write_text(json.dumps(config, indent=2))
        for container in containers:
            env.stop_container(container)
    return predictions


def evaluate(args, out, predictions):
    import docker
    # Reuse the existing harness compatibility hook for local instance images.
    import experiments.run_swebench_eval_repair
    from swebench.harness.run_evaluation import get_dataset_from_preds, load_swebench_dataset, run_instances, make_run_report
    data = 'outputs/swebench/swebench_verified_test_154.json'
    preds = {p['instance_id']: p for p in predictions}
    ids = [args.instance_id]
    run_id = out.name
    dataset = get_dataset_from_preds(data, 'test', ids, preds, run_id)
    if dataset:
        run_instances(preds, dataset, cache_level='none', clean=False,
                      force_rebuild=False, max_workers=1, run_id=run_id, timeout=1800)
    report = make_run_report(preds, load_swebench_dataset(data, 'test', ids), docker.from_env(), run_id)
    (out / 'report.json').write_text(Path(report).read_text())
    print(f'Report: {out / "report.json"}', flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--instance-id', default='django__django-10999')
    p.add_argument('--model-name', default='DeepSeek-V4-Flash-0731')
    p.add_argument('--agents', type=int, default=3,
                   help='1 for the identical-capability single-agent control; 3 for the native team')
    p.add_argument('--seed', type=int, default=0, help='Local RNG seed for voting ties; not a model sampling seed')
    p.add_argument('--skip-eval', action='store_true')
    args = p.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
    out = Path('outputs/swebench') / f'native_capable_a{args.agents}_{stamp}'
    out.mkdir(parents=True)
    print(f'Artifacts: {out}', flush=True)
    predictions = asyncio.run(generate(args, out))
    if not args.skip_eval:
        evaluate(args, out, predictions)


if __name__ == '__main__':
    main()
