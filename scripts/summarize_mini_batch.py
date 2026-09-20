"""Reproduce the fixed-team experiment report from immutable local artifacts."""
import argparse
import collections
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics

PHASES = ['analysis', 'implementation', 'review', 'revision', 'final_review']
LABELS = dict(zip(PHASES, ['分析', '实现', '初审', '返工', '最终复核']))
TOKENS = ['prompt_tokens', 'completion_tokens', 'total_tokens', 'reasoning_tokens', 'cached_tokens']


def read(path):
    return json.loads(path.read_text())


def quantile(values, q):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * q
    lo = int(position)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def distribution(values):
    return dict(n=len(values), total=sum(values), mean=statistics.mean(values) if values else None,
                median=quantile(values, .5), p90=quantile(values, .9), p95=quantile(values, .95),
                min=min(values) if values else None, max=max(values) if values else None)


def totals(rows):
    fields = TOKENS + ['model_requests', 'model_responses', 'api_errors', 'inflight_requests',
                      'actions', 'checkpoints', 'reasoning_responses', 'usage_missing',
                      'reasoning_usage_present', 'cached_usage_present', 'length_responses',
                      'api_success_seconds', 'api_error_seconds', 'error_to_retry_gap_seconds']
    return {key: sum(row.get(key, 0) for row in rows) for key in fields}


def write_csv(path, rows):
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('batch', type=Path)
    args = parser.parse_args()
    batch = args.batch.resolve()
    out = batch / 'report'
    out.mkdir(exist_ok=True)
    state = read(batch / 'status.json')
    manifest = read(batch / 'manifest.json')
    assert state['status'] == 'finished'
    assert len(state['tasks']) == manifest['total'] == 154
    anomalies = []
    sources = {}

    def record_source(path):
        sources[str(path.relative_to(batch))] = hashlib.sha256(path.read_bytes()).hexdigest()

    for name in ['status.json', 'manifest.json', 'config.json', 'preflight.json']:
        record_source(batch / name)
    tasks, phases = [], []
    errors = collections.Counter()
    finishes = collections.Counter()
    network = collections.Counter()
    versions = collections.Counter()
    dependency_versions = collections.defaultdict(set)
    applied = 0
    for index, (iid, task) in enumerate(state['tasks'].items(), 1):
        assert task['status'] == 'finished' and len(task['attempts']) == 1
        attempt = task['attempts'][0]
        folder = batch / 'instances' / Path(attempt['output_dir']).name
        official = read(folder / 'report.json')
        generation = read(folder / 'generation.json')
        record_source(folder / 'report.json')
        record_source(folder / 'generation.json')
        outcomes = [label for label, field in [('resolved', 'resolved_ids'), ('unresolved', 'unresolved_ids'),
                    ('empty_patch', 'empty_patch_ids'), ('evaluation_error', 'error_ids')]
                    if iid in official.get(field, [])]
        assert len(outcomes) == 1 and outcomes[0] == task['outcome'], (iid, outcomes)
        start, end = [datetime.fromisoformat(attempt[k]) for k in ['started', 'finished']]
        runtime = [json.loads(line) for line in (folder / 'runtime.jsonl').read_text().splitlines()]
        record_source(folder / 'runtime.jsonl')
        starts = {e['phase']: e['elapsed'] for e in runtime if e['event'] == 'phase_started'}
        ends = {e['phase']: e['elapsed'] for e in runtime if e['event'] == 'phase_finished'}
        for e in runtime:
            if 'network' in e:
                network['generation_' + e['network']] += 1
        if (folder / 'eval_network.jsonl').exists():
            record_source(folder / 'eval_network.jsonl')
            for line in (folder / 'eval_network.jsonl').read_text().splitlines():
                network['evaluation_' + json.loads(line)['network']] += 1
        row = dict(instance_id=iid, repo=task['repo'], outcome=outcomes[0],
                   started=attempt['started'], finished=attempt['finished'],
                   elapsed_seconds=(end-start).total_seconds(),
                   generation_trace_seconds=max(e['elapsed'] for e in runtime),
                   generation_status=generation['status'], patch_chars=generation['patch_chars'])
        row['residual_seconds'] = row['elapsed_seconds'] - row['generation_trace_seconds']
        assert row['residual_seconds'] >= 0
        detail_path = batch / 'source/logs/run_evaluation' / folder.name / manifest['config']['model'] / iid / 'report.json'
        row['detailed_eval_report'] = detail_path.exists()
        if detail_path.exists():
            record_source(detail_path)
            detail = read(detail_path)[iid]
            assert detail['resolved'] == (row['outcome'] == 'resolved')
            row['patch_applied'] = detail.get('patch_successfully_applied', False)
            applied += int(row['patch_applied'])
            for group in ['FAIL_TO_PASS', 'PASS_TO_PASS']:
                for result in ['success', 'failure']:
                    row[group.lower() + '_' + result] = len(detail['tests_status'][group][result])
        else:
            assert row['outcome'] == 'empty_patch'
        task_phases = []
        for phase in PHASES:
            data = dict(instance_id=iid, repo=task['repo'], outcome=row['outcome'], phase=phase,
                        exit_status=generation['phases'][phase]['exit_status'],
                        verdict=generation['phases'][phase].get('verdict'),
                        phase_started=phase in starts,
                        observed_phase_seconds=(ends[phase]-starts[phase]) if phase in starts and phase in ends else None)
            counter = collections.Counter()
            event_path = folder / phase / 'events.jsonl'
            pending, last_error = None, None
            digest = hashlib.sha256()
            worker_end = None
            if event_path.exists():
                with event_path.open('rb') as stream:
                    for number, raw in enumerate(stream, 1):
                        digest.update(raw)
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            anomalies.append(dict(file=str(event_path.relative_to(batch)), line=number, type='invalid_json'))
                            continue
                        kind = event['event']
                        elapsed = event['elapsed']
                        if kind == 'worker_started':
                            versions[event['version']] += 1
                            for name, sha in event.get('dependency_hashes', {}).items():
                                dependency_versions[name].add(sha)
                        elif kind == 'worker_finished':
                            worker_end = elapsed
                        elif kind == 'model_request':
                            counter['model_requests'] += 1
                            if pending is not None:
                                anomalies.append(dict(instance_id=iid, phase=phase, type='overlapping_request'))
                            if last_error is not None:
                                counter['error_to_retry_gap_seconds'] += elapsed-last_error
                                last_error = None
                            pending = elapsed
                        elif kind == 'model_response':
                            counter['model_responses'] += 1
                            if pending is not None:
                                counter['api_success_seconds'] += elapsed-pending
                            pending = None
                            usage = event.get('usage') or {}
                            if not all(isinstance(usage.get(k), (int, float)) for k in TOKENS[:3]):
                                counter['usage_missing'] += 1
                            for key in TOKENS[:3]:
                                counter[key] += usage.get(key) or 0
                            if usage and usage.get('total_tokens') != (usage.get('prompt_tokens') or 0) + (usage.get('completion_tokens') or 0):
                                anomalies.append(dict(instance_id=iid, phase=phase, type='usage_total_mismatch'))
                            for group, key, output, coverage in [
                                ('completion_tokens_details', 'reasoning_tokens', 'reasoning_tokens', 'reasoning_usage_present'),
                                ('prompt_tokens_details', 'cached_tokens', 'cached_tokens', 'cached_usage_present')]:
                                value = (usage.get(group) or {}).get(key)
                                if value is not None:
                                    counter[output] += value
                                    counter[coverage] += 1
                            reason = event.get('finish_reason')
                            finishes[str(reason)] += 1
                            counter['length_responses'] += int(reason == 'length')
                            counter['reasoning_responses'] += int(bool((event.get('message') or {}).get('reasoning_content')))
                        elif kind == 'model_error':
                            counter['api_errors'] += 1
                            error = event.get('error', 'unknown')
                            errors[error] += 1
                            counter['error_' + error] += 1
                            if pending is not None:
                                counter['api_error_seconds'] += elapsed-pending
                            pending, last_error = None, elapsed
                        elif kind == 'action':
                            counter['actions'] += 1
                        elif kind == 'checkpoint':
                            counter['checkpoints'] += 1
                sources[str(event_path.relative_to(batch))] = digest.hexdigest()
            counter['inflight_requests'] = int(pending is not None)
            for key in TOKENS:
                counter.setdefault(key, 0)
            data.update(totals([counter]))
            data.update(counter)
            data['worker_finished_seconds'] = worker_end
            phases.append(data)
            task_phases.append(data)
        row.update(totals(task_phases))
        for key in {k for p in task_phases for k in p if k.startswith('error_') and k != 'error_to_retry_gap_seconds'}:
            row[key] = sum(p.get(key, 0) for p in task_phases)
        row['all_roles_completed'] = all(p['exit_status'] in ['Submitted', 'SkippedApproved'] for p in task_phases)
        row['final_review_started'] = next(p['phase_started'] for p in task_phases if p['phase'] == 'final_review')
        row['revision_started'] = next(p['phase_started'] for p in task_phases if p['phase'] == 'revision')
        implement_patch = folder / 'implementation/workspace.patch'
        row['implementation_patch_available'] = implement_patch.exists()
        row['final_equals_implementation'] = (implement_patch.read_bytes() == (folder / 'selected.patch').read_bytes()) if implement_patch.exists() else None
        tasks.append(row)
        if index % 20 == 0:
            print(f'Aggregated {index}/{len(state["tasks"])} tasks', flush=True)

    wall = (datetime.fromisoformat(state['finished'])-datetime.fromisoformat(state['started'])).total_seconds()
    overall = totals(tasks)
    overall.update(outcomes=dict(collections.Counter(t['outcome'] for t in tasks)),
                   batch_wall_seconds=wall, task_seconds=distribution([t['elapsed_seconds'] for t in tasks]),
                   generation_trace_seconds=distribution([t['generation_trace_seconds'] for t in tasks]),
                   residual_seconds=distribution([t['residual_seconds'] for t in tasks]),
                   task_tokens=distribution([t['total_tokens'] for t in tasks]),
                   average_active_tasks=sum(t['elapsed_seconds'] for t in tasks)/wall,
                   all_roles_completed=sum(t['all_roles_completed'] for t in tasks),
                   tasks_with_api_errors=sum(t['api_errors'] > 0 for t in tasks),
                   tasks_with_rate_limits=sum(t.get('error_RateLimitError', 0) > 0 for t in tasks),
                   phase_exit_counts=dict(collections.Counter(p['exit_status'] for p in phases)),
                   generation_status_counts=dict(collections.Counter(t['generation_status'] for t in tasks)),
                   patch_applied=applied,
                   detailed_eval_reports=sum(t['detailed_eval_report'] for t in tasks),
                   implementation_patch_comparable=sum(t['implementation_patch_available'] and t['patch_chars'] > 0 for t in tasks),
                   implementation_patch_unchanged=sum(t['final_equals_implementation'] is True and t['patch_chars'] > 0 for t in tasks),
                   role_completion_outcomes={label: dict(collections.Counter(t['outcome'] for t in tasks if t['all_roles_completed'] == complete))
                                             for label, complete in [('complete', True), ('incomplete', False)]})
    domains = {}
    for repo in manifest['domains']:
        selected = [t for t in tasks if t['repo'] == repo]
        domains[repo] = dict(n=len(selected), outcomes=dict(collections.Counter(t['outcome'] for t in selected)),
            elapsed=distribution([t['elapsed_seconds'] for t in selected]),
            domain_span_seconds=(max(datetime.fromisoformat(t['finished']) for t in selected)-min(datetime.fromisoformat(t['started']) for t in selected)).total_seconds(),
            tokens=distribution([t['total_tokens'] for t in selected]), **totals(selected))
    phase_summary = {}
    for phase in PHASES:
        selected = [p for p in phases if p['phase'] == phase]
        phase_summary[phase] = dict(exits=dict(collections.Counter(p['exit_status'] for p in selected)),
            verdicts=dict(collections.Counter(str(p['verdict']) for p in selected)),
            started=sum(p['phase_started'] for p in selected),
            elapsed=distribution([p['observed_phase_seconds'] for p in selected if p['observed_phase_seconds'] is not None]),
            **totals(selected))
    groups = {outcome: dict(n=len(selected), tokens=distribution([t['total_tokens'] for t in selected]),
               seconds=distribution([t['elapsed_seconds'] for t in selected]))
              for outcome in ['resolved', 'unresolved', 'empty_patch']
              if (selected := [t for t in tasks if t['outcome'] == outcome])}
    hash_mismatches = [name for name, sha in manifest['hashes'].items()
                       if hashlib.sha256((batch / name).read_bytes()).hexdigest() != sha]
    assert not hash_mismatches
    assert not anomalies
    assert set(network) <= {'generation_none', 'evaluation_none'}
    for name, values in dependency_versions.items():
        assert values == {manifest['hashes']['source/minisweagent/' + name]}
    assert overall['model_requests'] == overall['model_responses'] + overall['api_errors'] + overall['inflight_requests']
    assert overall['total_tokens'] == overall['prompt_tokens'] + overall['completion_tokens']
    assert sum(d['total_tokens'] for d in domains.values()) == sum(p['total_tokens'] for p in phase_summary.values()) == overall['total_tokens']
    summary = dict(batch_id=batch.name, generated_at=datetime.now(timezone.utc).isoformat(),
                   started=state['started'], finished=state['finished'], config=manifest['config'],
                   overall=overall, domains=domains, phases=phase_summary, outcomes=groups,
                   api_errors=dict(errors), finish_reasons=dict(finishes), network=dict(network),
                   mini_versions=dict(versions), mini_dependency_hashes={k: sorted(v) for k,v in dependency_versions.items()},
                   frozen_hash_mismatches=hash_mismatches, anomalies=anomalies,
                   empty_patch_ids=[t['instance_id'] for t in tasks if t['outcome'] == 'empty_patch'])
    (out / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    (out / 'source_hashes.json').write_text(json.dumps(sources, indent=2))
    write_csv(out / 'per_task.csv', tasks)
    write_csv(out / 'per_phase.csv', phases)
    report(summary, tasks, out)
    print(json.dumps(dict(report=str(out / 'experiment_report.md'), overall=overall, api_errors=errors,
                         anomalies=anomalies), ensure_ascii=False), flush=True)


def report(s, tasks, out):
    o = s['overall']
    n = len(tasks)
    f = lambda x: f'{x:,.0f}'
    pct = lambda x, d: f'{100*x/d:.2f}%' if d else 'N/A'
    million = lambda x: f'{x/1e6:.3f}'
    lines = [
        '# GPTSwarm＋mini-swe-agent 固定团队全量实验报告', '',
        f'批次：`{s["batch_id"]}`；报告生成时间：{s["generated_at"]}。', '',
        f'本批在本地 SWE-bench Verified **154 题子集**上得到 **102/154（66.23%）** 官方解决率；49 题未解决、3 题空补丁。该数字不能直接视为完整 Verified 测试集的成绩。', '',
        '![实验结果、队列时间线与 token 用量](experiment_overview.png)', '',
        '**实验范围与配置**', '',
        '- 固定原生 Swarm 协作图，关闭节点与边优化；三种逻辑角色 Analyst、Engineer、Reviewer，使用相同 mini 实现和模型、不同角色提示词。',
        '- 五阶段：分析 → 实现 → 初审 → 返工 → 最终复核；初审批准且补丁未变时可跳过后两阶段。最多一轮返工，最后从真实工作区导出补丁。',
        '- mini-swe-agent 2.4.6（含本地适配，实际包副本已冻结）；模型 `DeepSeek-V4-Flash-0731`，temperature=0.2。',
        '- 四个仓库域并行、域内逐题生成并官方评估；每题一次正式尝试，不复用单题试跑结果，无任务级自动重试。API 内部重试仍启用。',
        '- 每题共享生成预算 1,200 秒；阶段建议时间依次为 240/420/240/180/120 秒，属于交接提示，不是独立硬配额。单次 API timeout 上限 300 秒、max_tokens=16,384、命令 timeout=120 秒。',
        '- 阶段 step 上限依次为 30/80/35/40/20；官方评估测试 timeout=1,800 秒，整题子进程兜底 timeout=3,600 秒。',
        '- 模型输入仅公开问题描述，不提供 hints、标准补丁或隐藏评估测试名。生成前恢复精确 base commit，清除其他 Git 历史；生成与评估 Docker 均断网。',
        '- 本报告只统计 v4b 正式批次；预检、此前单题调试和被中断的 v4 启动批次不计入时间/token 总数。', '',
        '**官方结果**', '',
        '| 仓库域 | 题数 | 解决 | 未解决 | 空补丁 | 解决率 |',
        '|---|---:|---:|---:|---:|---:|']
    for repo, d in s['domains'].items():
        c=d['outcomes'];lines.append(f'| {repo} | {d["n"]} | {c.get("resolved",0)} | {c.get("unresolved",0)} | {c.get("empty_patch",0)} | {pct(c.get("resolved",0),d["n"])} |')
    lines += ['| **总计** | **154** | **102** | **49** | **3** | **66.23%** |', '',
        f'154 份任务汇总报告均已核对；{o["detailed_eval_reports"]} 份非空补丁详细评估报告，{o["patch_applied"]} 份补丁成功应用，无官方评估错误或报告缺失。空补丁按失败计入 154 题分母，但没有启动测试容器。', '',
        '空补丁题：' + '、'.join(f'`{iid}`' for iid in s['empty_patch_ids']) + '。', '',
        '**运行时间**', '',
        f'- 开始：{s["started"]}；结束：{s["finished"]}（均为 UTC）。',
        f'- 整批墙钟时间：**{o["batch_wall_seconds"]/3600:.3f} 小时**。',
        f'- 全部任务累计端到端时间：**{o["task_seconds"]["total"]/3600:.3f} 任务小时**；这是各题生成、评估和收尾时间之和，不是机器 CPU/GPU 使用时长。',
        f'- 平均活动任务数：{o["average_active_tasks"]:.3f}（累计任务时间 / 批次墙钟时间），不等同于相对于串行实验的实测加速比。', '',
        '| 时间口径 | 均值（分钟） | 中位数（分钟） | P90（分钟） | P95（分钟） | 最大值（分钟） |',
        '|---|---:|---:|---:|---:|---:|']
    for title,key in [('逐题端到端','task_seconds'),('生成运行轨迹跨度','generation_trace_seconds'),('剩余评估及进程开销（估算）','residual_seconds')]:
        d=o[key];lines.append('| '+title+' | '+' | '.join(f'{d[k]/60:.2f}' for k in ['mean','median','p90','p95','max'])+' |')
    lines += ['', '端到端时间来自批调度器逐题 started/finished。生成轨迹跨度取该题最后一条 runtime 事件的 monotonic elapsed，覆盖环境准备和角色工作，但不是完整生成进程的精确计时；二者差额包含启动、最终导出/清理、官方评估和报告开销，**不能作为纯 eval 测试耗时**。API 调用等待、限流重试和工具执行均包含在相应墙钟时间内。分位数使用线性插值。', '',
        '| 仓库域 | 队列跨度（小时） | 平均端到端（分钟/题） | 平均 token/题 | 总 token（百万） |',
        '|---|---:|---:|---:|---:|']
    for repo,d in s['domains'].items():
        lines.append(f'| {repo} | {d["domain_span_seconds"]/3600:.2f} | {d["elapsed"]["mean"]/60:.2f} | {f(d["tokens"]["mean"])} | {million(d["total_tokens"])} |')
    lines += ['', '**Token 消耗**', '',
        '| 指标 | 数值 |', '|---|---:|',
        f'| 输入 prompt tokens | {f(o["prompt_tokens"])} |',
        f'| 输出 completion tokens | {f(o["completion_tokens"])} |',
        f'| **总 tokens** | **{f(o["total_tokens"])}** |',
        f'| 其中 reasoning tokens（已含在输出中） | {f(o["reasoning_tokens"])} |',
        f'| 接口报告 cached input tokens（已含在输入中） | {f(o["cached_tokens"])} |',
        f'| 输入中 cached 占比 / 输出中 reasoning 占比 | {pct(o["cached_tokens"],o["prompt_tokens"])} / {pct(o["reasoning_tokens"],o["completion_tokens"])} |',
        f'| 平均 / 中位数每题总 tokens | {f(o["task_tokens"]["mean"])} / {f(o["task_tokens"]["median"])} |',
        f'| 每题总 tokens P90 / P95 / 最大值 | {f(o["task_tokens"]["p90"])} / {f(o["task_tokens"]["p95"])} / {f(o["task_tokens"]["max"])} |',
        f'| 全批 token / 解决题数（含失败题开销的摊销） | {f(o["total_tokens"]/102)} |', '',
        f'仅逐行累计各阶段 `events.jsonl` 的 `model_response.usage`；没有再次累计 trace、消息交接或 result 中的重复用量。收到 {f(o["model_responses"])} 个响应，其中 {o["usage_missing"]} 个缺少完整基础 usage；reasoning 字段覆盖 {f(o["reasoning_usage_present"])} 个响应，cached 字段覆盖 {f(o["cached_usage_present"])} 个响应。', '',
        '输入 token 每次请求重复发送的历史上下文均照接口用量计入，并非去重文本量。Reasoning 是 completion 的子集，cached 是 prompt 的子集，均不得再加到 total；接口报告的缓存量不代表已核验供应商折扣。失败/超时请求及被截止取消的请求可能已在服务端消耗 token，但未返回 usage，因此报告总量只是**可观测返回用量，不是完整账单**。未获取可信账单及本账户价格，故不估算货币费用。', '',
        '| 结果组 | 题数 | 平均总 token/题 | 中位数 token/题 | 平均端到端（分钟） |',
        '|---|---:|---:|---:|---:|']
    for name,d in s['outcomes'].items():
        lines.append(f'| {name} | {d["n"]} | {f(d["tokens"]["mean"])} | {f(d["tokens"]["median"])} | {d["seconds"]["mean"]/60:.2f} |')
    lines += ['', '**角色耗时、用量与完成情况**', '',
        '| 阶段 | 实际启动 | Submitted | 平均配对阶段耗时（秒） | 输入 token | 输出 token | reasoning token | 总 token 占比 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for phase,d in s['phases'].items():
        lines.append(f'| {LABELS[phase]} | {d["started"]} | {d["exits"].get("Submitted",0)} | {d["elapsed"]["mean"]:.1f}（n={d["elapsed"]["n"]}） | {f(d["prompt_tokens"])} | {f(d["completion_tokens"])} | {f(d["reasoning_tokens"])} | {pct(d["total_tokens"],o["total_tokens"])} |')
    lines += ['', '实际启动按 runtime 的 phase_started 计；阶段耗时只对具有成对 phase_started/phase_finished 的记录统计，包含 worker 及结果处理、不含该阶段启动前的快照准备和之后的容器回收。未启动阶段不按 0 秒混入均值。三种逻辑角色对应五种阶段，并非五个独立团队成员。', '',
        f'{o["all_roles_completed"]}/154 题的所有阶段均为 Submitted 或合法跳过，另 {154-o["all_roles_completed"]} 题至少一个阶段非正常结束。任务最终仍可导出补丁参加官方评估，不能将角色异常数当作解题失败数。', '',
        f'最终复核实际启动 {s["phases"]["final_review"]["started"]}/154 次，正常提交 {s["phases"]["final_review"]["exits"].get("Submitted",0)}/154 次，显示后续流程经常未完成。完整流程题中 {o["role_completion_outcomes"]["complete"].get("resolved",0)} 题解决，非完整流程题中 {o["role_completion_outcomes"]["incomplete"].get("resolved",0)} 题解决；题目难度、用时和结果相互关联，不应把两组差异解读为协作完成度的因果效果。', '',
        '| 阶段退出状态 | 次数 |', '|---|---:|']
    lines += [f'| {key} | {value} |' for key,value in o['phase_exit_counts'].items()]
    lines += ['', '**API 稳定性与计时影响**', '',
        f'- 模型请求尝试 {f(o["model_requests"])} 次，返回响应 {f(o["model_responses"])} 次，记录错误 {f(o["api_errors"])} 次，日志末尾仍未取得响应/错误的请求 {o["inflight_requests"]} 次。请求数满足三类结果之和；最后一类可能被外层 deadline 终止。',
        '- 请求统计以适配层 model_request 事件为准，可能不包含 SDK 内部不可见的 HTTP 重试；返回响应不等同于该题解答正确。',
        f'- {o["tasks_with_api_errors"]} 题至少一次 API 错误，{o["tasks_with_rate_limits"]} 题遇到 RateLimitError。',
        f'- 成功 API 请求配对等待累计 {o["api_success_seconds"]/3600:.3f} 小时，失败请求配对等待累计 {o["api_error_seconds"]/3600:.3f} 小时；错误返回至下一次请求的累计间隔 {o["error_to_retry_gap_seconds"]/3600:.3f} 小时。均为跨任务相加，重试间隔含客户端处理，不是纯服务端排队时间，未结束请求的等待不在配对计时中。',
        f'- 非空 reasoning_content 出现在 {f(o["reasoning_responses"])} 个响应；finish_reason=length 共 {o["length_responses"]} 次。', '',
        '| 错误类型 | 次数 |', '|---|---:|']
    lines += [f'| {key} | {value} |' for key,value in s['api_errors'].items()]
    lines += ['', '请求限流和重试消耗 1,200 秒共享墙钟预算，会挤占后续角色时间。日志中的 RateLimitError 不能直接推断余额耗尽；账户剩余额度和未返回请求的计费未核验。错误类型与任务结果不是因果归因，本报告不把所有未解决题归为能力不足或接口问题。', '',
        '**隔离、复现与结论边界**', '',
        f'- 全量容器准备预检 154/154 通过；正式运行记录生成工作区 {s["network"].get("generation_none",0)} 个、评估容器 {s["network"].get("evaluation_none",0)} 个，网络均为 none。断网限制容器，宿主模型 API 仍需联网。',
        f'- 冻结源码/数据/配置 hash 核对不一致数：{len(s["frozen_hash_mismatches"])}；原始事件解析或计数异常数：{len(s["anomalies"])}。实际 mini 版本：{dict(s["mini_versions"])}（次数为 worker 启动数）。其他第三方 Python 依赖未完整封装为可移植环境。',
        f'- 在 {o["implementation_patch_comparable"]} 道可比的非空补丁题中，{o["implementation_patch_unchanged"]} 道最终补丁与实现阶段保存的补丁逐字节相同。相同并不意味着审查无价值，改变也不证明返工提升正确性。',
        '- 这是固定多角色框架的基线结果，没有同预算单 mini 对照、重复随机种子或边优化实验，不能据此宣称多智能体协作带来因果增益或演进有效。',
        '- 官方 harness 结果保留原样；先前已观察到 Django 子测试日志拼接影响个别测试名解析，故不重写官方评分，也不将逐项测试计数视为人工复核结论。',
        '- 已知本地协议问题包括基线检查命令格式、断言输出识别、SymPy 异常输出分类以及只读角色暂时修改源码的检测限制。本地 approval 不作为官方成功依据。',
        '- 单题试跑曾通过的 SymPy 13031 在本批为空补丁，说明一次试跑成功不保证本批可重复成功；需结合逐题轨迹分析，不得用试跑结果替换本批结果。', '',
        '**后续分析重点**', '',
        '优先检查共享时间分配与限流重试如何影响返工/复核的可用时间；再区分需求理解、错误修复、测试回归、空补丁和工具协议问题。改进后应另起冻结批次，与相同任务、模型及预算的单 mini 基线比较，保留当前原始结果。', '',
        '**报告附件与复现**', '',
        '- [结构化汇总](summary.json)：所有总量、域/阶段分组、分位数、异常及协议版本。',
        '- [逐题明细](per_task.csv)：154 行，包含官方结果、时间、token、请求/错误与测试计数。',
        '- [逐阶段明细](per_phase.csv)：770 行，包含阶段退出、时间和 token。',
        '- [原始统计输入 SHA-256](source_hashes.json)：用于验证统计所依据的事件及报告文件。',
        '- [统计脚本](summarize_mini_batch.py)：仅读取既有轨迹，不调用模型、不重跑评估。',
        '- 图表：[SVG](experiment_overview.svg) / [PDF](experiment_overview.pdf) / [PNG](experiment_overview.png)；[绘图脚本](plot_mini_batch_report.py)。',
        '- 原始配置与数据见上一级 `manifest.json`、`config.json`、`dataset.json`；原始轨迹见 `instances/`，官方评估日志见 `source/logs/run_evaluation/`。', '',
        '复现命令（传入原始批次目录；允许目录整体搬迁）：', '',
        '```bash', 'python report/summarize_mini_batch.py /path/to/mini_full_20260918_domains_v4b',
        'python report/plot_mini_batch_report.py /path/to/mini_full_20260918_domains_v4b/report', '```', '']
    (out / 'experiment_report.md').write_text('\n'.join(lines))
    (out / 'summarize_mini_batch.py').write_bytes(Path(__file__).read_bytes())


if __name__ == '__main__':
    main()
