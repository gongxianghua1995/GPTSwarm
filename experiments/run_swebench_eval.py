#!/usr/bin/env python
"""Run swebench harness evaluation, skipping env image building (instances already exist)."""

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
    # Fall back to original if image doesn't exist
    _orig_build_instance_image(test_spec, client, logger, nocache)

_db.build_instance_image = _patched_build_instance_image


def main():
    dataset_name = "outputs/swebench/swebench_verified_test_154.json"
    split = "test"
    predictions_path = "outputs/swebench/predictions_harness.json"
    run_id = "eval_001"
    max_workers = 4
    timeout = 1800

    resource.setrlimit(resource.RLIMIT_NOFILE, (4096, 4096))
    client = docker.from_env()

    # Load predictions
    with open(predictions_path) as f:
        predictions_list = json.load(f)
    predictions = {p[KEY_INSTANCE_ID]: p for p in predictions_list}

    # Get dataset (only instances with non-empty predictions)
    dataset = get_dataset_from_preds(dataset_name, split, None, predictions, run_id)
    full_dataset = load_swebench_dataset(dataset_name, split, None)
    print(f"Running {len(dataset)} unevaluated instances...", flush=True)

    if dataset:
        run_instances(
            predictions, dataset,
            cache_level="none", clean=False, force_rebuild=False,
            max_workers=max_workers, run_id=run_id, timeout=timeout,
        )

    # Generate report
    report_file = make_run_report(predictions, full_dataset, client, run_id)
    print(f"\nReport: {report_file}", flush=True)


if __name__ == "__main__":
    main()
