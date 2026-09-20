#!/usr/bin/env python
"""Evaluate the SWERepairAgent predictions on the full 154-case test split."""

import json
import resource
import docker

from swebench.harness.run_evaluation import (
    get_dataset_from_preds,
    load_swebench_dataset,
    run_instances,
    make_run_report,
    KEY_INSTANCE_ID,
)

import swebench.harness.docker_build as _db

_orig_build_instance_image = _db.build_instance_image

def _patched_build_instance_image(test_spec, client, logger, nocache):
    image_name = test_spec.instance_image_key
    try:
        client.images.get(image_name)
        if logger:
            logger.info(f"Image {image_name} already exists, skipping build.")
        return
    except docker.errors.ImageNotFound:
        pass
    _orig_build_instance_image(test_spec, client, logger, nocache)

_db.build_instance_image = _patched_build_instance_image


def main():
    dataset_name = "outputs/swebench/swebench_verified_test_154.json"
    split = "test"
    predictions_path = "outputs/swebench/predictions_repair.json"
    run_id = "eval_repair_001"
    max_workers = 4
    timeout = 1800

    resource.setrlimit(resource.RLIMIT_NOFILE, (4096, 4096))
    client = docker.from_env()

    with open(dataset_name) as f:
        instance_ids = [row["instance_id"] for row in json.load(f)]
    print(f"Evaluating {len(instance_ids)} retried instances...", flush=True)

    with open(predictions_path) as f:
        predictions_list = json.load(f)
    predictions = {
        p[KEY_INSTANCE_ID]: p for p in predictions_list
        if p[KEY_INSTANCE_ID] in set(instance_ids)
    }

    dataset = get_dataset_from_preds(dataset_name, split, instance_ids, predictions, run_id)
    full_dataset = load_swebench_dataset(dataset_name, split, instance_ids)
    print(f"Running {len(dataset)} unevaluated instances...", flush=True)

    if dataset:
        run_instances(
            predictions, dataset,
            cache_level="none", clean=False, force_rebuild=False,
            max_workers=max_workers, run_id=run_id, timeout=timeout,
        )

    report_file = make_run_report(predictions, full_dataset, client, run_id)
    print(f"\nReport: {report_file}", flush=True)


if __name__ == "__main__":
    main()
