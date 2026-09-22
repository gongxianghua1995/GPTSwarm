"""Read-only SWE-bench Pro report: deterministic retry replacement and all-attempt costs.

Requires the original and completed retry batches, including the pause snapshot.
Uses only Python's standard library. Does not call a model or run evaluation.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics

PHASES = ['analysis', 'implementation', 'review', 'revision', 'final_review']
LABELS = dict(zip(PHASES, ['分析', '实现', '初审', '返工', '最终复核']))
TOKENS = ['prompt_tokens', 'completion_tokens', 'total_tokens', 'reasoning_tokens', 'cached_tokens']
FIELDS = TOKENS + ['model_requests', 'model_responses', 'api_errors', 'inflight_requests',
    'actions', 'checkpoints', 'reasoning_responses', 'usage_missing', 'reasoning_usage_present',
    'cached_usage_present', 'length_responses', 'api_success_seconds', 'api_error_seconds',
    'error_to_retry_gap_seconds']


def quantile(values, q):
    if not values:
        return None
    values = sorted(values)
    p = (len(values) - 1) * q
    lo = int(p)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (p - lo)


def distribution(values):
    return dict(n=len(values), total=sum(values), mean=statistics.mean(values) if values else None,
                median=quantile(values, .5), p90=quantile(values, .9), p95=quantile(values, .95),
                min=min(values) if values else None, max=max(values) if values else None)


def totals(rows):
    return {k: sum(r.get(k, 0) for r in rows) for k in FIELDS}


def write_csv(path, rows):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
        writer.writeheader()
        writer.writerows(rows)


def seconds(start, end):
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def group(rows):
    return dict(n=len(rows), outcomes=dict(Counter(r['outcome'] for r in rows)),
                task_seconds=distribution([r['elapsed_seconds'] for r in rows]),
                task_tokens=distribution([r['total_tokens'] for r in rows]), **totals(rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('original', type=Path)
    parser.add_argument('retry', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    batches = [args.original.resolve(), args.retry.resolve()]
    out = args.output_dir.resolve()
    assert not any(out == b or b in out.parents for b in batches), 'Keep derived reports outside frozen batches'
    sources = {}

    def key(path, batch):
        return batch.name + '/' + str(path.relative_to(batch))

    def raw(path, batch):
        value = path.read_bytes()
        sources[key(path, batch)] = hashlib.sha256(value).hexdigest()
        return value

    def read(path, batch):
        return json.loads(raw(path, batch))

    states = [read(b / 'status.json', b) for b in batches]
    manifests = [read(b / 'manifest.json', b) for b in batches]
    for s in states:
        assert s['status'] == 'finished' and not s.get('worker_errors')
        assert all(t['status'] == 'finished' for t in s['tasks'].values())
    selection = read(batches[1] / 'selection.json', batches[1])
    retry_ids = {r['instance_id'] for r in selection['affected']}
    assert retry_ids == set(states[1]['tasks']) <= set(states[0]['tasks'])
    assert len(retry_ids) == selection['budget_exceeded_tasks']
    assert manifests[0]['config'] == manifests[1]['config']
    dataset = [{r['instance_id']: r for r in read(b / 'dataset.json', b)} for b in batches]
    assert set(dataset[0]) == set(states[0]['tasks']) and set(dataset[1]) == retry_ids
    assert all(dataset[0][iid] == dataset[1][iid] for iid in retry_ids)
    assert all(manifests[0]['images'][k] == v for k, v in manifests[1]['images'].items())
    changed = sorted(k for k in set(manifests[0]['hashes']) | set(manifests[1]['hashes'])
                     if manifests[0]['hashes'].get(k) != manifests[1]['hashes'].get(k))
    assert changed == sorted(manifests[1]['source_changes_from_original'])
    assert set(changed) == {'dataset.json', 'source/experiments/run_swebench_mini_batch.py'}
    for b, m in zip(batches, manifests):
        for name, sha in m['hashes'].items():
            assert hashlib.sha256(raw(b / name, b)).hexdigest() == sha, (b.name, name)
        for name in ['preflight.json', 'evaluation_controls.json', 'config.json', 'launch_audit.json']:
            read(b / name, b)
    preflight = [read(b / 'preflight.json', b) for b in batches]
    assert all(p['passed'] == p['total'] == len(s['tasks']) for p, s in zip(preflight, states))
    controls = read(batches[0] / 'evaluation_controls.json', batches[0])['controls']
    assert len(controls) == 8
    assert all(c['resolved'] == int(c['control'].startswith('gold_')) and
               c['unresolved'] == int(c['control'].startswith('base_')) for c in controls)
    pause_files = sorted(batches[1].glob('status_before_resume_*.json'))
    assert len(pause_files) == 1, 'This report handles one documented budget pause'
    paused = read(pause_files[0], batches[1])
    assert paused['status'] != 'finished' and paused['pause_reason']['reason'] == 'api_budget_exhausted'
    resume_audit = [json.loads(line) for line in raw(batches[1] / 'resume_audit.jsonl', batches[1]).splitlines()]
    attempts, phases, selections = [], [], []
    anomalies = []
    dependencies = defaultdict(set)

    for bi, (b, state, manifest) in enumerate(zip(batches, states, manifests)):
        for iid, task in state['tasks'].items():
            for ai, attempt in enumerate(task['attempts'], 1):
                selected = (bi == 1 or iid not in retry_ids) and ai == len(task['attempts'])
                interrupted = bool(attempt.get('interrupted'))
                assert not selected or not interrupted
                folder = b / 'instances' / Path(attempt['output_dir']).name
                runtime = [json.loads(line) for line in raw(folder / 'runtime.jsonl', b).splitlines()]
                starts = {e['phase']: e['elapsed'] for e in runtime if e['event'] == 'phase_started'}
                ends = {e['phase']: e['elapsed'] for e in runtime if e['event'] == 'phase_finished'}
                finished_phases = {e['phase']: e for e in runtime if e['event'] == 'phase_finished'}
                generation = read(folder / 'generation.json', b) if (folder / 'generation.json').exists() else {}
                outcome = attempt['outcome']
                row = dict(instance_id=iid, repo=task['repo'], batch_id=b.name, attempt=ai,
                           artifact_dir=key(folder, b), selected=selected, interrupted=interrupted,
                           cost_group='selected' if selected else ('interrupted_retry' if bi else 'superseded_original'),
                           outcome=outcome, started=attempt['started'], finished=attempt['finished'],
                           elapsed_seconds=seconds(attempt['started'], attempt['finished']),
                           generation_status=generation.get('status', 'interrupted_without_summary'),
                           patch_chars=generation.get('patch_chars'),
                           generation_trace_seconds=max(e['elapsed'] for e in runtime),
                           generation_network_none=0, evaluation_network_none=0,
                           detailed_eval_report=False, patch_applied=False, hard_budget_error=False)
                # Do not mistake an interrupted trace's residual for evaluation time.
                row['residual_seconds'] = None if interrupted else row['elapsed_seconds'] - row['generation_trace_seconds']
                assert row['residual_seconds'] is None or row['residual_seconds'] >= 0
                if (folder / 'report.json').exists():
                    report_data = read(folder / 'report.json', b)
                    labels = [label for label, field in [('resolved', 'resolved_ids'), ('unresolved', 'unresolved_ids'),
                              ('empty_patch', 'empty_patch_ids'), ('evaluation_error', 'error_ids')]
                              if iid in report_data.get(field, [])]
                    assert labels == [outcome], (iid, labels, outcome)
                    if ai == len(task['attempts']):
                        assert outcome == task['outcome']
                    detail_file = folder / 'eval_details.json'
                    if detail_file.exists():
                        detail = read(detail_file, b)[iid]
                        assert detail['resolved'] == (outcome == 'resolved')
                        row['infra_failure'] = bool(detail.get('infra_failure'))
                        row['infra_failure_reason'] = detail.get('infra_failure_reason')
                        row['detailed_eval_report'] = True
                        row['tests_status_available'] = 'tests_status' in detail
                        row['patch_applied'] = detail.get('patch_successfully_applied', False)
                        for g in ['FAIL_TO_PASS', 'PASS_TO_PASS']:
                            for result in ['success', 'failure']:
                                row[g.lower() + '_' + result] = len(detail['tests_status'][g][result]) if 'tests_status' in detail else None
                        assert 'tests_status' in detail or not row['patch_applied']
                        harness_dir = b / 'source/logs/run_evaluation' / folder.name / manifest['config']['model'].replace('/', '__') / iid
                        assert read(harness_dir / 'report.json', b)[iid] == detail
                        if 'tests_status' not in detail:
                            log = raw(harness_dir / 'run_instance.log', b).decode(errors='replace')
                            test_output = raw(harness_dir / 'test_output.txt', b).decode(errors='replace')
                            row['harness_logged_patch_applied'] = '>>>>> Applied Patch:' in log
                            row['eval_exception_category'] = (
                                'network_unreachable' if row['infra_failure_reason'] == 'network_unreachable' else
                                'go_build_or_setup_failed' if '[build failed]' in test_output or '[setup failed]' in test_output else
                                'no_parsed_test_results')
                    else:
                        assert outcome == 'empty_patch'
                else:
                    assert interrupted and not selected
                for e in runtime:
                    if 'network' in e:
                        assert e['network'] == 'none'
                        row['generation_network_none'] += 1
                if (folder / 'eval_network.jsonl').exists():
                    for line in raw(folder / 'eval_network.jsonl', b).splitlines():
                        assert json.loads(line)['network'] == 'none'
                        row['evaluation_network_none'] += 1
                selected_patch = folder / 'selected.patch'
                if selected_patch.exists():
                    patch = raw(selected_patch, b)
                    row['patch_sha256'] = hashlib.sha256(patch).hexdigest()
                    assert len(patch.decode()) == row['patch_chars']
                implementation_patch = folder / 'implementation/workspace.patch'
                row['implementation_patch_available'] = implementation_patch.exists()
                row['final_equals_implementation'] = (raw(implementation_patch, b) == raw(selected_patch, b)) if implementation_patch.exists() and selected_patch.exists() else None
                task_phases = []
                for phase in PHASES:
                    info = generation.get('phases', {}).get(phase, finished_phases.get(phase, {}))
                    data = {k: row[k] for k in ['instance_id', 'repo', 'batch_id', 'attempt', 'selected', 'interrupted', 'outcome']}
                    data.update(phase=phase, phase_started=phase in starts,
                        exit_status=info.get('exit_status', 'Interrupted' if phase in starts else 'NotStarted'),
                        verdict=info.get('verdict'),
                        observed_phase_seconds=ends[phase] - starts[phase] if phase in starts and phase in ends else None)
                    counter = Counter()
                    pending = last_error = None
                    event_file = folder / phase / 'events.jsonl'
                    if event_file.exists():
                        digest = hashlib.sha256()
                        with event_file.open('rb') as stream:
                            for number, line in enumerate(stream, 1):
                                digest.update(line)
                                try:
                                    e = json.loads(line)
                                except json.JSONDecodeError:
                                    anomalies.append(dict(file=key(event_file, b), line=number, kind='invalid_json'))
                                    continue
                                kind, elapsed = e['event'], e['elapsed']
                                if kind == 'worker_started':
                                    data['mini_version'] = e['version']
                                    assert e['version'] == manifest['worker_versions']['mini_swe_agent']
                                    for name, sha in e.get('dependency_hashes', {}).items():
                                        dependencies[name].add(sha)
                                        assert sha == manifest['hashes']['source/minisweagent/' + name]
                                elif kind == 'model_request':
                                    assert pending is None
                                    counter['model_requests'] += 1
                                    if last_error is not None:
                                        counter['error_to_retry_gap_seconds'] += elapsed - last_error
                                    pending, last_error = elapsed, None
                                elif kind in ['model_response', 'model_error']:
                                    assert pending is not None
                                    if kind == 'model_error':
                                        counter['api_errors'] += 1
                                        counter['error_' + e.get('error', 'unknown')] += 1
                                        counter['api_error_seconds'] += elapsed - pending
                                        last_error = elapsed
                                    else:
                                        counter['model_responses'] += 1
                                        counter['api_success_seconds'] += elapsed - pending
                                        usage = e.get('usage') or {}
                                        counter['usage_missing'] += int(not all(isinstance(usage.get(k), (int, float)) for k in TOKENS[:3]))
                                        for k in TOKENS[:3]:
                                            counter[k] += usage.get(k) or 0
                                        assert not usage or usage.get('total_tokens') == (usage.get('prompt_tokens') or 0) + (usage.get('completion_tokens') or 0)
                                        for category, field in [('completion_tokens_details', 'reasoning_tokens'), ('prompt_tokens_details', 'cached_tokens')]:
                                            value = (usage.get(category) or {}).get(field)
                                            if value is not None:
                                                counter[field] += value
                                                counter['reasoning_usage_present' if field == 'reasoning_tokens' else 'cached_usage_present'] += 1
                                        counter['reasoning_responses'] += int(bool((e.get('message') or {}).get('reasoning_content')))
                                        counter['length_responses'] += int(e.get('finish_reason') == 'length')
                                        counter['finish_' + str(e.get('finish_reason'))] += 1
                                    pending = None
                                elif kind in ['action', 'checkpoint']:
                                    counter['actions' if kind == 'action' else 'checkpoints'] += 1
                        sources[key(event_file, b)] = digest.hexdigest()
                    # Read only the hard-budget marker, never publish raw provider errors/credentials.
                    for logfile in (folder / phase).glob('*.log'):
                        if b'Budget has been exceeded' in raw(logfile, b):
                            row['hard_budget_error'] = True
                    counter['inflight_requests'] = int(pending is not None)
                    data.update(totals([counter]))
                    data.update(counter)
                    assert data['model_requests'] == data['model_responses'] + data['api_errors'] + data['inflight_requests']
                    task_phases.append(data)
                row.update(totals(task_phases))
                for k in {k for p in task_phases for k in p if k.startswith(('error_', 'finish_'))}:
                    row[k] = sum(p.get(k, 0) for p in task_phases)
                row['all_roles_completed'] = all(p['exit_status'] in ['Submitted', 'SkippedApproved'] for p in task_phases)
                attempts.append(row)
                phases.extend(task_phases)
                if len(attempts) % 40 == 0:
                    print(f'Aggregated {len(attempts)} attempts', flush=True)
    assert not anomalies, anomalies
    tasks = [r for r in attempts if r['selected']]
    selected_phases = [r for r in phases if r['selected']]
    assert len(tasks) == len({r['instance_id'] for r in tasks}) == manifests[0]['total']
    assert {r['instance_id'] for r in attempts if r['batch_id'] == batches[0].name and r['hard_budget_error']} == retry_ids
    assert not any(r['hard_budget_error'] for r in tasks)
    for r in tasks:
        iid = r['instance_id']
        selections.append(dict(instance_id=iid, repo=r['repo'], original_outcome=states[0]['tasks'][iid]['outcome'],
            selected_batch=r['batch_id'], selected_attempt=r['attempt'], selected_artifact_dir=r['artifact_dir'],
            final_outcome=r['outcome'], replacement=iid in retry_ids,
            selection_reason='explicit_api_budget_exceeded_latest_completed_retry' if iid in retry_ids else 'retain_original'))
    scopes = {'selected': group(tasks), 'all_attempts': group(attempts)}
    for name in ['superseded_original', 'interrupted_retry']:
        scopes[name] = group([r for r in attempts if r['cost_group'] == name])
    for label, b in zip(['original_batch', 'retry_batch'], batches):
        scopes[label] = group([r for r in attempts if r['batch_id'] == b.name])
    for k in FIELDS:
        assert abs(scopes['all_attempts'][k] - sum(scopes[g][k] for g in ['selected', 'superseded_original', 'interrupted_retry'])) < 1e-5
    domains = {}
    for repo in manifests[0]['domains']:
        rows = [r for r in tasks if r['repo'] == repo]
        domains[repo] = group(rows)
        domains[repo]['all_attempts'] = group([r for r in attempts if r['repo'] == repo])
        domains[repo]['retry_tasks'] = sum(r['repo'] == repo for r in selections if r['replacement'])
    phase_summary = {}
    for phase in PHASES:
        rows = [r for r in selected_phases if r['phase'] == phase]
        phase_summary[phase] = dict(exits=dict(Counter(r['exit_status'] for r in rows)),
            verdicts=dict(Counter(str(r['verdict']) for r in rows)), started=sum(r['phase_started'] for r in rows),
            elapsed=distribution([r['observed_phase_seconds'] for r in rows if r['observed_phase_seconds'] is not None]), **totals(rows))
    o = scopes['selected']
    o.update(generation_trace_seconds=distribution([r['generation_trace_seconds'] for r in tasks]),
             residual_seconds=distribution([r['residual_seconds'] for r in tasks]),
             all_roles_completed=sum(r['all_roles_completed'] for r in tasks),
             phase_exit_counts=dict(Counter(r['exit_status'] for r in selected_phases)),
             generation_status_counts=dict(Counter(r['generation_status'] for r in tasks)),
             role_completion_outcomes={label:dict(Counter(r['outcome'] for r in tasks if r['all_roles_completed'] == flag))
                                       for label,flag in [('complete',True), ('incomplete',False)]},
             patch_applied=sum(r['patch_applied'] for r in tasks),
             tests_status_available=sum(r.get('tests_status_available', False) for r in tasks),
             infra_failures=sum(r.get('infra_failure', False) for r in tasks),
             detailed_eval_reports=sum(r['detailed_eval_report'] for r in tasks),
             implementation_patch_comparable=sum(bool(r['implementation_patch_available'] and r['patch_chars']) for r in tasks),
             implementation_patch_unchanged=sum(bool(r['final_equals_implementation'] and r['patch_chars']) for r in tasks))
    for label, rows in [('selected', tasks), ('all_attempts', attempts)]:
        scopes[label].update(api_error_types={k[6:]:sum(r.get(k, 0) for r in rows) for k in sorted({k for r in rows for k in r if k.startswith('error_') and k != 'error_to_retry_gap_seconds'})},
            finish_reasons={k[7:]:sum(r.get(k, 0) for r in rows) for k in sorted({k for r in rows for k in r if k.startswith('finish_')})},
            tasks_with_api_errors=sum(r['api_errors'] > 0 for r in rows),
            tasks_with_rate_limits=sum(r.get('error_RateLimitError', 0) > 0 for r in rows),
            hard_budget_error_attempts=sum(r['hard_budget_error'] for r in rows),
            network={k:sum(r[k] for r in rows) for k in ['generation_network_none', 'evaluation_network_none']})
        assert scopes[label]['total_tokens'] == scopes[label]['prompt_tokens'] + scopes[label]['completion_tokens']
    assert sum(d['total_tokens'] for d in domains.values()) == sum(p['total_tokens'] for p in phase_summary.values()) == o['total_tokens']
    intervals = [(states[0]['started'], states[0]['finished']), (paused['started'], paused['finished']), (states[1]['started'], states[1]['finished'])]
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    timing = dict(started=states[0]['started'], finished=states[1]['finished'],
        campaign_wall_seconds=seconds(states[0]['started'], states[1]['finished']),
        original_started=states[0]['started'], original_finished=states[0]['finished'],
        original_wall_seconds=seconds(states[0]['started'], states[0]['finished']),
        retry_initial_started=paused['started'], retry_resumed_started=states[1]['started'],
        pause_started=paused['finished'], retry_finished=states[1]['finished'],
        retry_wall_seconds=seconds(paused['started'], states[1]['finished']),
        pause_seconds=seconds(paused['finished'], states[1]['started']),
        active_controller_intervals=merged, active_controller_seconds=sum(seconds(a,z) for a,z in merged))
    summary = dict(schema_version=1, generated_at=datetime.now(timezone.utc).isoformat(),
        original_batch=batches[0].name, retry_batch=batches[1].name, config=manifests[0]['config'],
        worker_versions=manifests[0]['worker_versions'], timing=timing, scopes=scopes, domains=domains,
        phases=phase_summary, outcome_groups={v:group([r for r in tasks if r['outcome'] == v]) for v in sorted({r['outcome'] for r in tasks})},
        retry_policy=dict(selected_tasks=len(retry_ids), replaced_original_outcomes=selection['budget_exceeded_outcomes'],
            rule='Use the latest completed retry for every preselected ID regardless of outcome; otherwise retain original.',
            source_changes=changed, resume_probe_tokens=sum(r.get('api_probe_total_tokens', 0) for r in resume_audit)),
        validation=dict(frozen_hash_mismatches=[], anomalies=anomalies, dataset_records_equal=True,
            selected_image_ids_equal=True, selected_reports_match=True, preflight_passed=[p['passed'] for p in preflight],
            evaluation_controls_passed=len(controls),
            mini_versions=dict(Counter(r['mini_version'] for r in phases if 'mini_version' in r)),
            mini_dependency_hashes={k:sorted(v) for k,v in dependencies.items()}))
    summary['evaluation_exceptions'] = [{k:r.get(k) for k in ['instance_id','repo','artifact_dir','outcome','patch_applied','tests_status_available','infra_failure','infra_failure_reason','harness_logged_patch_applied','eval_exception_category']} for r in tasks if not r['patch_applied'] or r.get('infra_failure')]
    # Freeze the scope and log categories described by this dated report.
    assert len(tasks) == 216 and len(attempts) == 308 and len(retry_ids) == 89
    assert Counter(r['eval_exception_category'] for r in summary['evaluation_exceptions']) == {
        'go_build_or_setup_failed':10, 'no_parsed_test_results':3, 'network_unreachable':1}
    assert all(r['harness_logged_patch_applied'] for r in summary['evaluation_exceptions'])
    out.mkdir(parents=True, exist_ok=True)
    for name, data in [('summary.json', summary), ('source_hashes.json', sources)]:
        (out / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    for name, rows in [('per_task.csv', tasks), ('per_phase.csv', selected_phases), ('per_attempt.csv', attempts),
                       ('per_attempt_phase.csv', phases), ('selection.csv', selections),
                       ('evaluation_exceptions.csv', summary['evaluation_exceptions'])]:
        write_csv(out / name, rows)
    report(summary, out)
    (out / Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    print(json.dumps(dict(output=str(out), timing=timing,
        scopes={k:{f:v[f] for f in ['n','outcomes','total_tokens']} for k,v in scopes.items()}), indent=2))


def report(s, out):
    o, a, t = s['scopes']['selected'], s['scopes']['all_attempts'], s['timing']
    n, resolved = o['n'], o['outcomes'].get('resolved', 0)
    fmt = lambda v: f'{v:,.0f}'
    pct = lambda v,d: f'{v/d*100:.2f}%' if d else 'N/A'
    lines = ['# GPTSwarm＋mini-swe-agent：SWE-bench Pro 固定团队实验报告', '',
        f'报告生成时间：{s["generated_at"]}。原始批次 `{s["original_batch"]}`，额度异常补跑批次 `{s["retry_batch"]}`。', '',
        f'本地 SWE-bench Pro **test 划分 {n} 题**，按预先记录的基础设施补跑替换规则合并后，解决 **{resolved}/{n}（{pct(resolved,n)}）**；未解决 {o["outcomes"].get("unresolved",0)} 题，无空补丁。汇总 error_ids 为 0，但详细评估中有 {o["infra_failures"]} 题网络基础设施失败、共 {n-o["tests_status_available"]} 题无逐项测试结果，均已按未解决保留在分母。此成绩属于本地四域子集及本项目 Pro 评估适配，不是完整公开 Pro 数据集或一次无故障运行的成绩。', '',
        f'整个实验历时 **{t["campaign_wall_seconds"]/3600:.3f} 小时**，其中暂停等待额度 **{t["pause_seconds"]/3600:.3f} 小时**。最终采用的 {n} 次尝试记录 **{fmt(o["total_tokens"])} tokens**；包含故障废弃尝试在内的全部 {a["n"]} 次尝试记录 **{fmt(a["total_tokens"])} tokens**。', '',
        '![结果、运行时间线与资源消耗](experiment_overview.png)', '',
        '## 1. 实验范围与配置', '',
        '- 数据来自既有 `swebench_pro_split.json` 的 `families.*.test`：Ansible 63、Flipt 54、OpenLibrary 60、Webclients 39。smoke/opt、预检、评估器对照和先前试跑均不进入正式成绩分母，也不复用其他框架预测。',
        '- 保留原生 Swarm / CompositeGraph / Graph / Node 协作结构，固定节点和边，不运行优化。三种逻辑角色 Analyst、Engineer、Reviewer 均由 mini-swe-agent 实现，角色提示词、权限和交接内容不同。',
        '- 五阶段：分析 → 实现 → 初审 → 返工 → 最终复核，每阶段新建 mini worker；Engineer 两阶段接续同一可写工作区，Analyst 使用干净副本，Reviewer 使用候选补丁副本及独立基线。最多一轮返工；初审批准且补丁未变时可合法跳过后两阶段。最终从真实工作区导出补丁。',
        '- mini-swe-agent 2.4.6（冻结实际依赖源码和本地适配）、Python 3.11.15、swebench 5.0.2；模型 `DeepSeek-V4-Flash-0731`，temperature=0.2。',
        '- 每题共享生成预算 1,200 秒，含环境准备；阶段建议时间 240/420/240/180/120 秒，step 上限 30/80/35/40/20。单次输出上限 16,384 tokens、API timeout 300 秒、命令 timeout 120 秒、report 预算 60 秒。阶段建议时间不是独立硬配额。',
        '- 评估测试 timeout 1,800 秒；整题子进程保护 timeout 3,600 秒。四域并行、域内逐题生成后评估；补跑 Ansible 等待原域队列结束。API 内部重试启用。',
        '- 模型可见题目及公开 requirements/interface，不提供 gold patch、test patch、隐藏测试名或准备命令。生成镜像位于 `/app`，保留工具链 PATH 和已安装依赖；恢复精确基线并清除主仓库及子模块的其他 Git 历史。',
        '- 生成、基线检查、评审及评估容器均 `network=none`，模型 API 由宿主调用。', '',
        '## 2. 评估协议与结果', '',
        '采用 SWE-bench 5.x harness 加本项目 Pro TestSpec 与 Python/Go/Jest 日志解析适配，并非 stock Verified 评估入口。补丁应用后仅恢复指定测试文件，避免整仓 reset/checkout 擦除候选修改；隐藏测试准备失败不得算通过，Jest 使用镜像内工具且缺失测试不得算通过。Reviewer approval 和本地 check 结果不作为最终解题成功依据。', '',
        '| 仓库域 | 题数 | 解决 | 未解决 | 解决率 | 额度补跑题数 |',
        '|---|---:|---:|---:|---:|---:|']
    for repo,d in s['domains'].items():
        lines.append(f'| {repo} | {d["n"]} | {d["outcomes"].get("resolved",0)} | {d["outcomes"].get("unresolved",0)} | {pct(d["outcomes"].get("resolved",0),d["n"])} | {d["retry_tasks"]} |')
    lines += [f'| **总计** | **{n}** | **{resolved}** | **{n-resolved}** | **{pct(resolved,n)}** | **{s["retry_policy"]["selected_tasks"]}** |', '',
        f'{n} 份汇总结果、详细报告与最终选中尝试及状态文件一致，无报告缺失。其中 {o["tests_status_available"]} 份含 tests_status，另外 {n-o["tests_status_available"]} 份未进入逐项结果判定。详细报告的 patch_successfully_applied 字段只有 {o["patch_applied"]} 份为 true；该字段同时依赖有效测试日志，不能把 false 直接解释成 git apply 失败。14 题的 run_instance.log 均记录了 Applied Patch。', '',
        '**需要单独标出的评估问题：**', '',
        '- Flipt 10 题：日志出现 Go build/setup failed，未形成逐项测试结果。编译/依赖失败是否由候选修改引入仍需逐题核对基线，不能只按字段判为补丁应用失败。',
        '- Webclients 3 题：日志有开始/结束标记及 wrapper 调用，但没有可解析的逐项测试结果。实际 Jest 子进程及输出采集原因尚未确定，不能归为已完成正常测试后的失败。',
        '- OpenLibrary 1 题：详细报告 infra_failure=true、reason=network_unreachable；测试收集时尝试下载外部 schema，容器断网后域名解析失败。必须保留断网约束，后续应验证离线镜像依赖或 fixture 是否完整，不能据此判断补丁正确与否。',
        '- 评估封装只要取得详细 report.json 且 resolved=false 就写入 unresolved，因此 error_ids=0 **不表示没有基础设施异常**。本报告不改变原评分，也不删除这些题；见 [14 题逐项清单](evaluation_exceptions.csv)。', '',
        '## 3. 额度故障、补跑与确定性合并', '',
        '| 口径 | 题/尝试数 | 解决 | 未解决 | 空补丁 | 中断 |',
        '|---|---:|---:|---:|---:|---:|']
    for label,key in [('原始批次','original_batch'),('补跑全部尝试','retry_batch'),('最终选中','selected')]:
        d=s['scopes'][key];c=d['outcomes']
        lines.append(f'| {label} | {d["n"]} | {c.get("resolved",0)} | {c.get("unresolved",0)} | {c.get("empty_patch",0)} | {c.get("process_error",0)} |')
    lines += ['',
        '- 原批次 216 题完成后为 61 解决、70 未解决、85 空补丁。根据原始日志明确的 `Budget has been exceeded` 标记选出 89 题（85 空补丁＋4 未解决）；仅有普通 RateLimitError、随后成功重试的题不因此入选。选择证据在冻结的 `selection.json` 中。',
        '- 补跑第一次完成 51 题后再次因硬额度错误暂停，3 题正在执行而被中断、35 题尚未启动。额度恢复后保留已完成的 51 题，重新执行这 38 题，最终 89 题得到 39 解决、50 未解决；86 题各 1 次尝试，3 题各 2 次，共 92 次补跑尝试。',
        '- **全部 89 个入选 ID 一律使用最后完成的补跑结果，无论好坏；其余 127 题保留原结果。** 不从多次尝试中挑最好补丁，不将旧轨迹、旧补丁或评估反馈提供给补跑模型。原始记录保留原样。',
        '- 补跑与原批的数据记录、镜像 ID、agent/prompt/评估器冻结源码及配置一致；仅数据子集与调度器文件不同。调度器增加域队列等待和明确额度耗尽时暂停保护。',
        '- 最终 100 个成功由原批保留的 61 个和补跑的 39 个构成。分数变化包含服务恢复与新模型采样的影响，不能称为算法改进收益。', '',
        '## 4. 运行时间', '',
        '| 时间点/跨度（UTC） | 数值 |', '|---|---|',
        f'| 原批开始 → 结束 | {t["original_started"]} → {t["original_finished"]} |',
        f'| 补跑首次开始 | {t["retry_initial_started"]} |',
        f'| 补跑暂停 → 恢复 | {t["pause_started"]} → {t["retry_resumed_started"]} |',
        f'| 补跑最终结束 | {t["retry_finished"]} |',
        f'| 原批墙钟跨度 | {t["original_wall_seconds"]/3600:.3f} 小时 |',
        f'| 补跑墙钟跨度（含暂停） | {t["retry_wall_seconds"]/3600:.3f} 小时 |',
        f'| **整个实验墙钟跨度** | **{t["campaign_wall_seconds"]/3600:.3f} 小时** |',
        f'| 暂停等待额度 | {t["pause_seconds"]/3600:.3f} 小时 |',
        f'| 合并调度器活动区间（排除暂停、消除重叠） | {t["active_controller_seconds"]/3600:.3f} 小时 |',
        f'| 最终选中尝试累计端到端时间 | {o["task_seconds"]["total"]/3600:.3f} 任务小时 |',
        f'| 全部尝试累计端到端时间 | {a["task_seconds"]["total"]/3600:.3f} 任务小时 |', '',
        '补跑 status.json 的 started 在恢复时被覆盖，因此首次开始和暂停时刻取恢复前快照。原批与补跑部分重叠，两批跨度不可直接相加。调度器活动区间仍含排队、工具、eval 等等待，不是模型持续运算时长；任务小时也不是 CPU/GPU 时长。', '',
        '| 时间口径 | 均值（分） | 中位数（分） | P90（分） | P95（分） | 最大值（分） |',
        '|---|---:|---:|---:|---:|---:|']
    for label,d in [('最终选中逐题端到端',o['task_seconds']), ('选中生成轨迹跨度',o['generation_trace_seconds']),
                    ('选中剩余评估及进程开销（估算）',o['residual_seconds']), ('全部尝试端到端',a['task_seconds'])]:
        lines.append('| '+label+' | '+' | '.join(f'{d[k]/60:.2f}' for k in ['mean','median','p90','p95','max'])+' |')
    lines += ['', '端到端来自调度器 started/finished；生成轨迹跨度取最后 runtime 事件的 monotonic elapsed。两者差值包含启动、导出、清理和评估，**不能解释为纯 eval 测试耗时**。中断尝试不统计这一差值。分位数使用线性插值。', '',
        '| 域 | 平均选中端到端（分/题） | 平均 token/题 | 选中 tokens（百万） | 全部尝试 tokens（百万） |', '|---|---:|---:|---:|---:|']
    for repo,d in s['domains'].items():
        lines.append(f'| {repo} | {d["task_seconds"]["mean"]/60:.2f} | {fmt(d["task_tokens"]["mean"])} | {d["total_tokens"]/1e6:.3f} | {d["all_attempts"]["total_tokens"]/1e6:.3f} |')
    lines += ['', '## 5. Token 消耗：选中成绩与全部成本', '',
        '| 指标 | 最终选中 216 次 | 全部 308 次尝试 |', '|---|---:|---:|']
    for label,k in [('输入 prompt tokens','prompt_tokens'),('输出 completion tokens','completion_tokens'),('**总 tokens**','total_tokens'),
                    ('其中 reasoning tokens（含于输出）','reasoning_tokens'),('其中 cached tokens（含于输入）','cached_tokens')]:
        lines.append(f'| {label} | {fmt(o[k])} | {fmt(a[k])} |')
    lines += ['', '| 成本组成（互斥，可相加） | 次数 | 总 tokens | 累计任务小时 |', '|---|---:|---:|---:|']
    for label,k in [('最终选中','selected'),('被替换的原批故障尝试','superseded_original'),('补跑被中断的尝试','interrupted_retry')]:
        d=s['scopes'][k];lines.append(f'| {label} | {d["n"]} | {fmt(d["total_tokens"])} | {d["task_seconds"]["total"]/3600:.3f} |')
    lines += ['',
        f'- 最终选中：平均每题 {fmt(o["task_tokens"]["mean"])} tokens，中位数 {fmt(o["task_tokens"]["median"])}；P90 {fmt(o["task_tokens"]["p90"])}、P95 {fmt(o["task_tokens"]["p95"])}、最大 {fmt(o["task_tokens"]["max"])}。',
        f'- 包含失败题的成功摊销：选中用量 / 100 = {fmt(o["total_tokens"]/resolved)} tokens/解决题；全部尝试用量 / 100 = {fmt(a["total_tokens"]/resolved)} tokens/解决题。',
        f'- 全部尝试 cached/input = {pct(a["cached_tokens"],a["prompt_tokens"])}，reasoning/output = {pct(a["reasoning_tokens"],a["completion_tokens"])}。',
        f'- 全部 {fmt(a["model_responses"])} 个返回响应中，基础 usage 缺失 {a["usage_missing"]} 个，reasoning 字段覆盖 {fmt(a["reasoning_usage_present"])} 个、cached 字段覆盖 {fmt(a["cached_usage_present"])} 个。',
        f'- 恢复前 API 探针另外记录 {s["retry_policy"]["resume_probe_tokens"]} tokens，不计入任务尝试；只把这项已知探针加上时为 {fmt(a["total_tokens"]+s["retry_policy"]["resume_probe_tokens"])} tokens，仍不等于账户完整账单。', '',
        '用量仅逐行累计 events.jsonl 中 model_response.usage，不重复累加 trace/result。每次请求重发历史上下文按接口用量计入；reasoning 是 completion 子集，cached 是 prompt 子集，均不能再加进 total。失败/超时/取消请求可能已在服务端消耗 token 却未返回 usage，故这里是**可观测返回用量**；不含早期开发调试或未记录探针，不推算货币费用。', '',
        '| 最终结果组 | 题数 | 平均 token/题 | 中位数 token/题 | 平均端到端（分） |', '|---|---:|---:|---:|---:|']
    for label,d in s['outcome_groups'].items():
        lines.append(f'| {label} | {d["n"]} | {fmt(d["task_tokens"]["mean"])} | {fmt(d["task_tokens"]["median"])} | {d["task_seconds"]["mean"]/60:.2f} |')
    lines += ['', '## 6. 角色轨迹、耗时与完成情况', '',
        '以下均以最终选中 216 次尝试为口径；全部尝试的阶段明细另附。', '',
        '| 阶段 | 实际启动 | Submitted | 合法跳过 | 平均配对耗时（秒） | 输入 tokens | 输出 tokens | reasoning tokens | 总 token 占比 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for phase,d in s['phases'].items():
        lines.append(f'| {LABELS[phase]} | {d["started"]} | {d["exits"].get("Submitted",0)} | {d["exits"].get("SkippedApproved",0)} | {d["elapsed"]["mean"]:.1f}（n={d["elapsed"]["n"]}） | {fmt(d["prompt_tokens"])} | {fmt(d["completion_tokens"])} | {fmt(d["reasoning_tokens"])} | {pct(d["total_tokens"],o["total_tokens"])} |')
    lines += ['', '阶段耗时仅统计成对 phase_started/phase_finished，含 worker 和结果处理；未启动阶段不按 0 秒混入平均值。三种逻辑角色对应五个阶段，并非五名独立成员。', '',
        f'全部阶段 Submitted 或合法跳过的题有 **{o["all_roles_completed"]}/{n}**，另 **{n-o["all_roles_completed"]}** 题至少一个阶段未正常完成。最终复核启动 {s["phases"]["final_review"]["started"]} 次、正常提交 {s["phases"]["final_review"]["exits"].get("Submitted",0)} 次。完整流程中解决 {o["role_completion_outcomes"]["complete"].get("resolved",0)} 题，未完整流程中解决 {o["role_completion_outcomes"]["incomplete"].get("resolved",0)} 题；角色异常不等于最终补丁失败。', '',
        '| 阶段退出状态 | 次数 |', '|---|---:|']
    lines += [f'| {k} | {v} |' for k,v in o['phase_exit_counts'].items()]
    lines += ['',
        f'可比的 {o["implementation_patch_comparable"]} 题中，{o["implementation_patch_unchanged"]} 题最终补丁与实现阶段补丁逐字节相同。审查未改补丁不能据此认定无价值，发生修改也不能证明收益。难度、用时和完成度相互关联，没有对照不能从这些分组作因果判断。', '',
        '## 7. API 稳定性与预算影响', '',
        '| 指标 | 最终选中 | 全部尝试 |', '|---|---:|---:|']
    for label,k in [('模型请求尝试','model_requests'),('返回响应','model_responses'),('API 错误','api_errors'),
                    ('末尾未收到响应/错误的请求','inflight_requests'),('至少一次 API 错误的尝试','tasks_with_api_errors'),
                    ('至少一次 RateLimitError 的尝试','tasks_with_rate_limits'),('非空 reasoning_content 响应','reasoning_responses'),
                    ('finish_reason=length','length_responses')]:
        lines.append(f'| {label} | {fmt(o[k])} | {fmt(a[k])} |')
    lines += ['', '| API 错误类型 | 最终选中 | 全部尝试 |', '|---|---:|---:|']
    for k in a['api_error_types']:
        lines.append(f'| {k} | {o["api_error_types"].get(k,0)} | {a["api_error_types"][k]} |')
    lines += ['',
        f'全部尝试的成功请求配对等待累计 {a["api_success_seconds"]/3600:.3f} 小时，失败请求等待 {a["api_error_seconds"]/3600:.3f} 小时，错误返回到下一次请求的间隔 {a["error_to_retry_gap_seconds"]/3600:.3f} 小时。均为跨任务相加，重试间隔含客户端处理；未结束请求等待不在配对计时内。请求数逐阶段核对为响应＋错误＋未结束请求，不含 SDK 内部不可见重试。', '',
        f'最终选中尝试均未出现明确额度耗尽标记；全部尝试中 {a["hard_budget_error_attempts"]} 次有该标记。硬额度错误以日志文本与选择审计判定，不能仅从 RateLimitError 类名推断。其余限流、超时、工具运行仍消耗共享 1,200 秒，会压缩返工和复核窗口。记录中存在 reasoning 内容及 usage，不支持“模型没有思考”的结论；这些计数也不能证明思路正确。', '',
        '## 8. 隔离、复现与结论边界', '',
        f'- 原批生成镜像预检 {s["validation"]["preflight_passed"][0]}/216，通过后补跑复用一致镜像的 89 条证据。初始扫描发现的 42 条镜像构建差异经定向复检修复：32 条 Go go.work.sum、3 条 OpenLibrary 测试初始化、7 条 Webclients sandbox.js。保留安装依赖，只恢复允许的构建改动。',
        '- 每域一题 gold patch 应通过、base patch 应失败，8/8 评估器对照符合预期。对照与预检不调用模型、不计入正式成绩。8 条对照只验证抽样通路，不等于已证明 216 题的解析器和测试集合完全无误。',
        f'- 最终选中记录生成网络检查 {o["network"]["generation_network_none"]} 条、eval 检查 {o["network"]["evaluation_network_none"]} 条；全部尝试分别 {a["network"]["generation_network_none"]} 和 {a["network"]["evaluation_network_none"]} 条，全部 network=none。次数为轨迹检查记录，不等同于去重后的物理容器数。',
        '- 原批和补跑 manifest 冻结文件逐项 SHA-256 核对无差异，实际 worker mini 依赖 hash 与冻结副本一致；选中结果与详细 eval 记录一致，事件解析和请求/用量守恒检查无异常。其他 Python 依赖未完整封装为可移植环境。',
        '- 数据仅为本地四域 test 子集。与此前 Verified 154 题的任务/仓库/评估不同，两个解决率不能直接用于衡量能力升降。',
        '- 本实验没有同预算单 mini、不同随机种子或启用边优化的对照；只能作为固定多角色架构基线，不能证明协作增益或演进有效。',
        '- 既有只读角色协议仍有暂时修改后恢复的检测限制，本地检查输出分类也可能误判；这些与共享预算挤占应继续结合逐题轨迹分析。未解决 116 题不能一概归为模型能力不足或基础设施问题。', '',
        '后续应另起冻结批次：先按需求理解、修改遗漏、回归、工具/协议异常分组分析失败，再在同一任务集、模型和预算下运行单 mini 与固定团队对照。当前报告保留故障、补跑成本和原始评估结论，不用新试跑覆盖。', '',
        '## 9. 附件与复现', '',
        '- [结构化汇总](summary.json)：配置、双口径成本、域/阶段结果、时间及审计。',
        f'- [逐题明细](per_task.csv)：最终选中 {n} 行；[逐阶段明细](per_phase.csv)：{n*5} 行。',
        f'- [全部尝试](per_attempt.csv)：{a["n"]} 行；[全部尝试阶段](per_attempt_phase.csv)：{a["n"]*5} 行，包含被替换和中断的开销。',
        '- [结果选择映射](selection.csv)：逐题原结果、替换理由、最终来源与结果。',
        '- [评估异常明细](evaluation_exceptions.csv)：缺少逐项测试结果的 14 题、原始标记和日志分类，不含隐藏测试内容。',
        '- [统计输入 SHA-256](source_hashes.json)：路径以批次目录名开头，文件位于本机 outputs/swebench/ 下；不附原始日志、模型轨迹、数据集隐藏字段或密钥。',
        '- [统计脚本](summarize_mini_pro_report.py)；[绘图脚本](plot_mini_pro_report.py)；图表 [SVG](experiment_overview.svg) / [PDF](experiment_overview.pdf) / [PNG](experiment_overview.png)。',
        '- [附件清单与 SHA-256](artifact_manifest.json)。统计只读原始批次，不请求模型、不重跑 eval；绘图只需报告附件及 matplotlib。', '',
        '在 GPTSwarm 项目根目录执行：', '', '```bash',
        'python scripts/summarize_mini_pro_report.py \\',
        f'  outputs/swebench/{s["original_batch"]} \\',
        f'  outputs/swebench/{s["retry_batch"]} \\',
        '  --output-dir docs/experiments/mini_pro_20260920',
        'python scripts/plot_mini_pro_report.py docs/experiments/mini_pro_20260920',
        '```', '',
        '统计脚本也可使用本目录副本，输入两批原始目录可整体搬迁。暂停时间依赖补跑目录中的 status_before_resume 快照；不可只用恢复后的 started。CSV 中空白 residual 表示中断未形成完整评估跨度，空白阶段耗时表示无成对事件，并非 0 秒。', '']
    (out / 'experiment_report.md').write_text('\n'.join(lines), encoding='utf-8')


if __name__ == '__main__':
    main()
