"""Select the existing Pro test split from a locally cached public dataset."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-arrow', required=True, type=Path)
    parser.add_argument('--split-json', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    from datasets import Dataset
    split = json.loads(args.split_json.read_text())
    selected = [(iid, domain) for domain, value in split['families'].items() for iid in value['test']]
    ids = dict(selected)
    assert len(ids) == len(selected) == split['counts']['test']
    other = {iid for family in split['families'].values() for name, values in family.items()
             if name != 'test' for iid in values}
    assert not set(ids) & other
    rows = [dict(r, benchmark='swebench_pro', experiment_domain=ids[r['instance_id']])
            for r in Dataset.from_file(str(args.source_arrow)) if r['instance_id'] in ids]
    assert len(rows) == len(ids)
    rows.sort(key=lambda r: (r['repo'], r['instance_id']))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    provenance = dict(source='ScaleAI/SWE-bench_Pro local cached test.arrow',
        source_sha256=hashlib.sha256(args.source_arrow.read_bytes()).hexdigest(),
        split_sha256=hashlib.sha256(args.split_json.read_bytes()).hexdigest(),
        output_sha256=hashlib.sha256(args.output.read_bytes()).hexdigest(),
        selection='existing families.*.test only; no smoke/opt examples',
        total=len(rows), domains=dict(Counter(r['repo'] for r in rows)))
    args.output.with_suffix('.provenance.json').write_text(json.dumps(provenance, indent=2))
    print(json.dumps(provenance), flush=True)


if __name__ == '__main__':
    main()
