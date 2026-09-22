"""Offline Pro evaluation via swebench 5.x and explicit Pro TestSpecs.

The adapters preserve the existing MetaGPT Pro evaluation protocol. This is
the SWE-bench harness with custom Pro parsers, not the Verified evaluation.
"""
import argparse
import json
from pathlib import Path
from unittest.mock import patch
import uuid

from experiments.swebench_pro_spec import make_test_spec, register_pro_parser


def evaluate(record, out, prediction, *, timeout=1800):
    import docker
    import swebench.harness.run_evaluation as harness
    iid = record['instance_id']
    model = prediction['model_name_or_path'].replace('/', '__')
    run_id = out.name
    report_path = harness.RUN_EVALUATION_LOG_DIR / run_id / model / iid / 'report.json'
    status = 'empty_patch'
    detail = None
    if prediction.get('model_patch', '').strip():
        register_pro_parser()
        spec = make_test_spec(record)

        def offline_container(test_spec, client, run_id, logger):
            container = client.containers.create(
                image=test_spec.image, name='gptswarm_pro_eval_' + uuid.uuid4().hex[:12],
                user='root', detach=True, network_mode='none', working_dir='/app',
                entrypoint='/bin/bash', command=['-c', 'exec tail -f /dev/null'],
                labels={'gptswarm.run': out.name})
            try:
                container.start()
                container.reload()
                mode = container.attrs['HostConfig']['NetworkMode']
                networks = list(container.attrs['NetworkSettings']['Networks'])
                if mode != 'none' or any(n != 'none' for n in networks):
                    raise RuntimeError('Pro evaluation container is not offline')
                with (out / 'eval_network.jsonl').open('a') as stream:
                    stream.write(json.dumps(dict(container=container.name, network=mode, networks=networks)) + '\n')
                return container
            except Exception:
                container.remove(force=True)
                raise

        with patch.object(harness, 'CONTAINER_WORKDIR', '/app'), patch.object(harness, 'create_container', offline_container):
            harness.run_instance(spec, prediction, docker.from_env(), run_id, timeout, False, False, None)
        if report_path.exists():
            detail = json.loads(report_path.read_text())[iid]
            status = 'resolved' if detail.get('resolved') else 'unresolved'
        else:
            status = 'error'
    report = dict(total_instances=1, submitted_instances=1, benchmark='swebench_pro',
                  evaluator='swebench-5.x-with-Pro-TestSpec-and-parsers', schema_version=2,
                  completed_ids=[iid] if status in {'resolved', 'unresolved'} else [])
    for label in ['resolved', 'unresolved', 'empty_patch', 'error']:
        report[label + '_ids'] = [iid] if status == label else []
        report[label + '_instances'] = int(status == label)
    report['completed_instances'] = len(report['completed_ids'])
    report['detailed_report'] = str(report_path.resolve()) if detail is not None else None
    (out / 'report.json').write_text(json.dumps(report, indent=2))
    if detail is not None:
        (out / 'eval_details.json').write_text(json.dumps({iid: detail}, indent=2))
    print(json.dumps(dict(instance_id=iid, outcome=status)), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-path', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--instance-id', required=True)
    args = parser.parse_args()
    record = next(r for r in json.loads(Path(args.data_path).read_text()) if r['instance_id'] == args.instance_id)
    out = Path(args.output_dir).resolve()
    prediction = next(p for p in json.loads((out / 'predictions.json').read_text()) if p['instance_id'] == args.instance_id)
    evaluate(record, out, prediction)


if __name__ == '__main__':
    main()
