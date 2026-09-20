"""Frozen, resumable fixed-team experiment: one sequential worker per repository."""
import argparse
import asyncio
import collections
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False))
    tmp.replace(path)


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


def classify(out, iid, returncode):
    generation = read_json(out / 'generation.json')
    report = read_json(out / 'report.json')
    if iid in report.get('resolved_ids', []):
        outcome = 'resolved'
    elif iid in report.get('unresolved_ids', []):
        outcome = 'unresolved'
    elif generation.get('status') in {'execution_error', 'patch_export_error', 'cancelled'}:
        outcome = 'generation_error'
    elif iid in report.get('empty_patch_ids', []):
        outcome = 'empty_patch'
    else:
        outcome = 'evaluation_error' if report or generation else 'process_error'
    return dict(outcome=outcome, returncode=returncode,
                generation_status=generation.get('status'),
                generation_error=generation.get('error'),
                patch_chars=generation.get('patch_chars'), phases=generation.get('phases', {}))


def cleanup(run_id):
    # Only containers bearing this exact task's label belong to this launcher.
    found = subprocess.run(['docker', 'ps', '-aq', '--filter', 'label=gptswarm.run=' + run_id],
                           capture_output=True, text=True, timeout=60, check=True)
    ids = found.stdout.split()
    if ids:
        subprocess.run(['docker', 'rm', '-f', *ids], capture_output=True, timeout=60, check=True)
    return ids


def prepare(args):
    batch = Path(args.batch_dir).resolve()
    batch.mkdir(parents=True, exist_ok=False)
    snapshot = batch / 'source'
    snapshot.mkdir()
    for directory in ('swarm', 'experiments', 'config'):
        shutil.copytree(ROOT / directory, snapshot / directory,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.env'))
    # Freeze the actual installed mini implementation, including local adapters.
    probe = subprocess.check_output([args.mini_python, '-c',
        'import minisweagent; print(minisweagent.__path__[0])'], text=True)
    mini_source = Path(probe.strip().splitlines()[-1])
    shutil.copytree(mini_source, snapshot / 'minisweagent',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.env'))
    shutil.copyfile(args.data_path, batch / 'dataset.json')
    shutil.copyfile(args.config, batch / 'config.json')
    rows = read_json(batch / 'dataset.json')
    ids = [r['instance_id'] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate instance IDs')
    images = subprocess.check_output(['docker', 'image', 'ls', '--no-trunc', '--format',
                                      '{{.Repository}}:{{.Tag}} {{.ID}}'], text=True, timeout=120)
    installed = dict(line.split() for line in images.splitlines())
    required = {}
    for iid in ids:
        for tag in ('swebench/sweb.eval.x86_64.' + iid.replace('__', '_1776_') + ':latest',
                    'sweb.eval.x86_64.' + iid + ':latest'):
            if tag not in installed:
                raise ValueError('Required offline image missing: ' + tag)
            required[tag] = installed[tag]
    hashes = {str(p.relative_to(batch)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(snapshot.rglob('*')) if p.is_file()}
    for name in ('config.json', 'dataset.json'):
        hashes[name] = hashlib.sha256((batch / name).read_bytes()).hexdigest()
    domains = {repo: [r['instance_id'] for r in rows if r['repo'] == repo]
               for repo in sorted({r['repo'] for r in rows})}
    manifest = dict(created=now(), batch_id=batch.name, domains=domains, total=len(rows),
                    python=sys.executable, mini_python=str(Path(args.mini_python).absolute()),
                    credential_env=str(ROOT / '.env'), hashes=hashes, images=required,
                    config=read_json(batch / 'config.json'),
                    strategy='one sequential generation+official-eval queue per repository',
                    retries=0, network_mode='none', previous_pilot_results_reused=False,
                    task_process_timeout_seconds=3600)
    write_json(batch / 'manifest.json', manifest)
    (batch / 'instances').mkdir()
    (batch / 'task_logs').mkdir()
    print(json.dumps(dict(batch_dir=str(batch), domains={k: len(v) for k, v in domains.items()})), flush=True)


def run(batch):
    lockfile = (batch / 'batch.lock').open('w')
    fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    manifest = read_json(batch / 'manifest.json')
    for relative, expected in manifest['hashes'].items():
        if hashlib.sha256((batch / relative).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Frozen source/data changed: ' + relative)
    load_dotenv(manifest['credential_env'])
    env = os.environ.copy()
    env['PYTHONPATH'] = str(batch / 'source')
    env['PYTHONUNBUFFERED'] = '1'
    state_path = batch / 'status.json'
    state = read_json(state_path) or dict(tasks={iid: dict(repo=repo, status='pending', attempts=[])
        for repo, ids in manifest['domains'].items() for iid in ids})
    state.update(pid=os.getpid(), started=now(), status='running', total=manifest['total'])
    mutex = threading.Lock()
    stop = threading.Event()

    def persist():
        state['updated'] = now()
        state['counts'] = dict(collections.Counter(t['status'] for t in state['tasks'].values()))
        state['outcomes'] = dict(collections.Counter(t.get('outcome') for t in state['tasks'].values()
                                                    if t['status'] == 'finished'))
        state['domains'] = {repo: dict(collections.Counter(state['tasks'][iid]['status'] for iid in ids))
                            for repo, ids in manifest['domains'].items()}
        write_json(state_path, state)

    def domain_worker(repo, ids):
        for iid in ids:
            if stop.is_set():
                return
            task = state['tasks'][iid]
            if task['status'] == 'finished':
                continue
            # On explicit resume retain unfinished artifacts, never silently overwrite.
            if task['attempts']:
                old = task['attempts'][-1]
                pid = old.get('pid')
                if task['status'] == 'running' and pid and Path(f'/proc/{pid}').exists():
                    raise RuntimeError('Previous task process still exists; cannot resume')
                cleanup(Path(old['output_dir']).name)
                old['interrupted'] = True
            number = len(task['attempts']) + 1
            run_id = f"{batch.name}__{iid}__a{number}"
            out = batch / 'instances' / run_id
            attempt = dict(started=now(), output_dir=str(out))
            proc = None
            with mutex:
                task['attempts'].append(attempt)
                task['status'] = 'running'
                persist()
            try:
                command = [manifest['python'], '-u', '-m', 'experiments.run_swebench_mini_swarm',
                           '--mini-python', manifest['mini_python'], '--data-path', str(batch / 'dataset.json'),
                           '--config', str(batch / 'config.json'), '--instance-id', iid, '--output-dir', str(out)]
                with (batch / 'task_logs' / (run_id + '.log')).open('w') as log:
                    proc = subprocess.Popen(command, cwd=batch / 'source', env=env, stdout=log,
                                            stderr=subprocess.STDOUT, start_new_session=True)
                    with mutex:
                        attempt['pid'] = proc.pid
                        persist()
                    deadline = time.monotonic() + manifest['task_process_timeout_seconds']
                    while proc.poll() is None:
                        if stop.wait(2) or time.monotonic() > deadline:
                            raise TimeoutError('Batch stopped or task process limit reached')
                result = classify(out, iid, proc.returncode)
            except Exception as exc:
                result = dict(outcome='process_error', error=type(exc).__name__)
            finally:
                if proc and proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                try:
                    removed = cleanup(run_id)
                    attempt['leftover_containers_removed'] = removed
                except Exception as exc:
                    attempt['cleanup_error'] = type(exc).__name__
            with mutex:
                attempt.update(finished=now(), **result)
                task.update(status='interrupted' if stop.is_set() else 'finished', **result)
                with (batch / 'results.jsonl').open('a') as stream:
                    stream.write(json.dumps(dict(instance_id=iid, repo=repo, **attempt)) + '\n')
                persist()
            print(json.dumps(dict(instance_id=iid, repo=repo, **result)), flush=True)

    def halt(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, halt)
    signal.signal(signal.SIGINT, halt)
    persist()
    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(manifest['domains'])) as pool:
        pending = {pool.submit(domain_worker, repo, ids) for repo, ids in manifest['domains'].items()}
        while pending:
            done, pending = concurrent.futures.wait(pending, timeout=30,
                                    return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                try:
                    future.result()
                except Exception as exc:
                    failures.append(type(exc).__name__)
                    stop.set()
            with mutex:
                persist()
    state['status'] = 'interrupted' if stop.is_set() else 'finished'
    state['worker_errors'] = failures
    state['finished'] = now()
    persist()


async def preflight(batch):
    from swarm.environment.agents.swe_bench.mini_runtime import MiniRuntime
    manifest = read_json(batch / 'manifest.json')
    config = read_json(batch / 'config.json')
    records = read_json(batch / 'dataset.json')
    semaphore = asyncio.Semaphore(len(manifest['domains']))
    results = []

    async def check(record):
        async with semaphore:
            out = batch / 'preflight' / (batch.name + '__' + record['instance_id'])
            out.mkdir(parents=True, exist_ok=False)
            runtime = MiniRuntime(record, config, out, manifest['mini_python'])
            result = dict(instance_id=record['instance_id'], repo=record['repo'])
            try:
                await runtime.prepare()
                exported = await runtime.export(include_tests=True)
                if exported['patch']:
                    raise RuntimeError('Prepared workspace is not clean')
                result['status'] = 'passed'
            except Exception as exc:
                result.update(status='error', error=type(exc).__name__, detail=str(exc)[:1000])
            finally:
                await runtime.close()
            results.append(result)
            write_json(batch / 'preflight.json', dict(total=len(records), checked=len(results),
                       passed=sum(r['status'] == 'passed' for r in results), results=results))
            if len(results) % 10 == 0 or result['status'] != 'passed':
                print(json.dumps(dict(checked=len(results), **result)), flush=True)

    await asyncio.gather(*(check(record) for record in records))
    failures = [r for r in results if r['status'] != 'passed']
    print(json.dumps(dict(preflight_passed=len(results)-len(failures), failed=len(failures))), flush=True)
    if failures:
        raise RuntimeError('Offline preparation preflight failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-dir', required=True)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--preflight-only', action='store_true')
    parser.add_argument('--data-path', default=str(ROOT / 'outputs/swebench/swebench_verified_test_154.json'))
    parser.add_argument('--config', default=str(ROOT / 'config/swebench/mini_fixed.json'))
    parser.add_argument('--mini-python', default=os.environ.get('MINISWE_PYTHON', sys.executable))
    args = parser.parse_args()
    if args.prepare_only:
        prepare(args)
    elif args.preflight_only:
        asyncio.run(preflight(Path(args.batch_dir).resolve()))
    else:
        run(Path(args.batch_dir).resolve())


if __name__ == '__main__':
    main()
