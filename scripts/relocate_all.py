#!/usr/bin/env python3
"""For each instance that errored in eval_001: run its docker image, relocate
patch hunks against the real source (scripts/relocate_hunks.py inside the
container), verify with `git apply --check`, and write the reconciled patches
into outputs/swebench/predictions_harness_fixed.json."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
RELOC = ROOT + "/scripts/relocate_hunks.py"


def is_test_path(path):
    p = path.lower()
    base = p.rsplit("/", 1)[-1]
    return ("/tests/" in p or "/test/" in p or p.startswith("tests/")
            or base.startswith("test_") or base.endswith("_test.py"))


def strip_test_files(patch):
    """Drop per-file sections that modify test files: the harness applies the
    official test patch itself, so model edits to tests only cause conflicts."""
    import re as _re
    parts = _re.split(r"(?m)^(?=diff --git )", patch)
    kept = []
    for part in parts:
        if not part.strip():
            continue
        m = _re.search(r"(?m)^\+\+\+ b?/?(\S+)", part)
        if m and is_test_path(m.group(1)):
            continue
        kept.append(part)
    return "".join(kept)


def image_for(iid, images):
    cands = [
        "swebench/sweb.eval.x86_64.%s:latest" % iid.replace("__", "_1776_"),
        "sweb.eval.x86_64.%s:latest" % iid,
    ]
    for c in cands:
        if c in images:
            return c
    return None


def process(iid, patch, images):
    img = image_for(iid, images)
    if img is None:
        return iid, None, "NO_IMAGE"
    patch = strip_test_files(patch)
    if not patch.strip():
        return iid, "", "EMPTY_AFTER_TEST_STRIP"
    pfile = "/tmp/reloc_%s.patch" % iid
    with open(pfile, "w") as f:
        f.write(patch)
    cmd = [
        "docker", "run", "--rm",
        "-v", pfile + ":/tmp/p.patch:ro",
        "-v", RELOC + ":/tmp/reloc.py:ro",
        img, "bash", "-c",
        "cd /testbed && python3 /tmp/reloc.py > /tmp/fixed.patch 2>/tmp/reloc.log; "
        "if git apply --check /tmp/fixed.patch >/dev/null 2>&1; then echo APPLY_OK; "
        "elif patch --dry-run --batch --fuzz=5 -p1 -i /tmp/fixed.patch >/dev/null 2>&1; then echo APPLY_FUZZ; "
        "else echo APPLY_FAIL; fi; "
        "echo '===PATCH==='; cat /tmp/fixed.patch; echo '===LOG==='; cat /tmp/reloc.log",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return iid, None, "TIMEOUT"
    outp = r.stdout
    if "===PATCH===" not in outp:
        return iid, None, "ERROR: " + (outp + r.stderr)[:200]
    status = outp.split("===PATCH===")[0].strip().splitlines()[-1] if outp.split("===PATCH===")[0].strip() else "?"
    fixed = outp.split("===PATCH===", 1)[1].split("===LOG===", 1)[0]
    fixed = fixed.lstrip("\n")
    if not fixed.endswith("\n"):
        fixed += "\n"
    log = outp.split("===LOG===", 1)[1] if "===LOG===" in outp else ""
    nomatch = sum(1 for l in log.splitlines() if l.startswith(("NOMATCH", "MISSING_FILE")))
    return iid, fixed, "%s nomatch_hunks=%d" % (status, nomatch)


def main():
    report_path = Path(ROOT) / 'DeepSeek-V4-Flash-0731.eval_001.json'
    if not report_path.exists():
        report_path = Path(ROOT) / 'outputs/swebench/archive/root_reports' / report_path.name
    rep = json.loads(report_path.read_text())
    error_ids = rep["error_ids"]
    preds_path = ROOT + "/outputs/swebench/predictions_harness_fixed.json"
    preds = json.load(open(preds_path))
    byid = {p["instance_id"]: p for p in preds}

    images = set(subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True, text=True).stdout.split())

    results = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(process, iid, byid[iid]["model_patch"], images): iid
                for iid in error_ids if iid in byid}
        for fut in futs:
            pass
        for fut in list(futs):
            iid, fixed, status = fut.result()
            results[iid] = status
            print("%-40s %s" % (iid, status), flush=True)
            if fixed is not None:
                byid[iid]["model_patch"] = fixed

    json.dump(preds, open(preds_path, "w"), indent=2)
    ok = sum(1 for s in results.values() if s.startswith("APPLY_OK"))
    fuzz = sum(1 for s in results.values() if s.startswith("APPLY_FUZZ"))
    fail = len(results) - ok - fuzz
    print("\nSummary: APPLY_OK=%d APPLY_FUZZ=%d other=%d / %d" % (ok, fuzz, fail, len(results)))
    json.dump(results, open(ROOT + "/outputs/swebench/relocate_status.json", "w"), indent=2)


if __name__ == "__main__":
    main()
