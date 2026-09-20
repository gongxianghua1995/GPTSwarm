#!/usr/bin/env python
"""Rerun swebench harness evaluation on the instances that errored in eval_001,
using the repaired patches (predictions_harness_fixed.json), under run_id eval_002."""

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

# Monkey-patch build_instance_image to skip env image check when instance image exists
import swebench.harness.docker_build as _db

_orig_build_instance_image = _db.build_instance_image

def _patched_build_instance_image(test_spec, client, logger, nocache):
    """Skip building if instance image already exists (ignore env image)."""
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
    predictions_path = "outputs/swebench/predictions_harness_fixed.json"
    prev_report = "DeepSeek-V4-Flash-0731.eval_001.json"
    run_id = "eval_002"
    max_workers = 4
    timeout = 1800

    resource.setrlimit(resource.RLIMIT_NOFILE, (4096, 4096))
    client = docker.from_env()

    with open(prev_report) as f:
        instance_ids = json.load(f)["error_ids"]
    print(f"Rerunning {len(instance_ids)} errored instances from eval_001...", flush=True)

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
