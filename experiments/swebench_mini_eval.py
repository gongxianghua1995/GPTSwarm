"""Official Verified harness with explicit, inspected offline containers."""
import json
from pathlib import Path
from unittest.mock import patch


def evaluate(data_path, out, prediction):
    import docker
    from docker.models.containers import ContainerCollection
    from swebench.harness.run_evaluation import (
        get_dataset_from_preds, load_swebench_dataset, run_instances, make_run_report)
    # Existing compatibility hook reuses installed instance images.
    import experiments.run_swebench_eval_repair

    original = ContainerCollection.create

    def offline_create(collection, *args, **kwargs):
        kwargs['network_mode'] = 'none'
        kwargs['labels'] = dict(kwargs.get('labels') or {}, **{'gptswarm.run': out.name})
        container = original(collection, *args, **kwargs)
        try:
            container.reload()
            network = container.attrs['HostConfig']['NetworkMode']
            if network != 'none':
                raise RuntimeError('Evaluation container is not offline')
            with (out / 'eval_network.jsonl').open('a') as stream:
                stream.write(json.dumps(dict(container=container.name, network=network)) + '\n')
            return container
        except Exception:
            container.remove(force=True)
            raise

    ids = [prediction['instance_id']]
    predictions = {ids[0]: prediction}
    run_id = out.name
    dataset = get_dataset_from_preds(str(data_path), 'test', ids, predictions, run_id)
    with patch.object(ContainerCollection, 'create', offline_create):
        if dataset:
            run_instances(predictions, dataset, cache_level='none', clean=False,
                          force_rebuild=False, max_workers=1, run_id=run_id, timeout=1800)
    report = make_run_report(predictions, load_swebench_dataset(str(data_path), 'test', ids),
                             docker.from_env(), run_id)
    (out / 'report.json').write_text(Path(report).read_text())
    print(f'Report: {out / "report.json"}', flush=True)
