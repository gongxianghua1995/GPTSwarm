# SWE-bench 固定团队基线

本分支增加 GPTSwarm 的 SWE-bench 适配，以及基于 mini-swe-agent 的固定多角色执行器。当前正式入口是 `experiments.run_swebench_mini_swarm` 和 `experiments.run_swebench_mini_batch`。

## 实验报告

[2026-09-18 全量固定团队实验](experiments/mini_full_20260918/experiment_report.md)：本地 SWE-bench Verified 154 题子集，102 题解决（66.23%），整批 22.20 小时；已返回 API usage 合计 206,801,947 tokens。该结果不代表完整 Verified 测试集成绩，也不能替代同预算单 mini 对照实验。

报告目录包含逐题/逐阶段 CSV、结构化汇总、统计输入 SHA-256 和图表；不包含任务数据、gold patch、原始模型消息、凭据或 Docker 日志。原始实验记录保留在运行机器的 `outputs/` 和 `logs/`，不进入 Git。

## 架构与入口

保留原生 `Swarm → CompositeGraph → Graph → Node` 调度。三个逻辑角色使用同一 mini 实现、不同提示词，执行分析、实现、初审、返工和最终复核五个阶段；不优化边或节点，不使用独立候选加随机选择作为本批基线。

- [固定团队设计、预算、协议与历史验证](swebench_mini_fixed_baseline.md)
- [迁移审计](swebench_migration_audit_20260918.md)
- [早期轨迹复核](swebench_mini_trace_review_20260918.md)
- [较早的原生执行器基线](swebench_native_baseline.md)

`run_swebench.py`、`run_swebench_eval*.py`、`run_swebench_failed.py`、`run_swebench_repair.py` 及补丁修整脚本保留用于追溯较早实验；它们不代表当前固定 mini 团队的推荐流程。旧结果及镜像清单文档是当时快照，不能覆盖上面的正式报告。正式 mini 评估目前复用 `run_swebench_eval_repair.py` 中的镜像复用兼容钩子。

## 运行准备

需要可用的 Docker、SWE-bench 数据和已准备好的对应生成/评估镜像。数据 JSON 默认位于 `outputs/swebench/swebench_verified_test_154.json`，也可通过 `--data-path` 指定。仓库只发布代码和派生报告；不会随 Git 下载数据集或镜像。

GPTSwarm 主进程需要项目依赖及 `docker`、`swebench`、`python-dotenv`。mini worker 可以使用独立 Python 环境，实际实验使用 mini-swe-agent 2.4.6、Python 3.11；统计及绘图另需 `matplotlib`。报告记录了实际 mini 关键文件 hash，实验依赖包含本地适配；仅安装同版本上游包不保证字节级复现。完整运行环境未打包在本仓库。

在本机环境变量或被 Git 忽略的 `.env` 中配置 `OPENAI_API_KEY`、`OPENAI_BASE_URL`（或 `OPENAI_API_BASE`），并设置：

```bash
export MINISWE_PYTHON=/path/to/mini-environment/bin/python
```

预算和模型配置见 [`config/swebench/mini_fixed.json`](../config/swebench/mini_fixed.json)。共享生成预算 1,200 秒，阶段秒数只作为交接提示，单次 API timeout 上限 300 秒、max_tokens 16,384。生成/评估容器均为 `network=none`；模型 API 由宿主调用。

## 单题与按域批跑

在项目根目录，用已安装 GPTSwarm 依赖的 Python 执行：

```bash
python -m experiments.run_swebench_mini_swarm \
  --mini-python "$MINISWE_PYTHON" \
  --data-path /path/to/swebench_verified_test_154.json \
  --instance-id django__django-10999

# 新批次目录必须不存在；准备时冻结源码、实际 mini 包、数据与配置。
python -m experiments.run_swebench_mini_batch \
  --prepare-only --batch-dir outputs/swebench/my_batch \
  --data-path /path/to/swebench_verified_test_154.json \
  --mini-python "$MINISWE_PYTHON"

# 使用冻结代码预检；在项目根目录给出明确的 PYTHONPATH。
PYTHONPATH="$PWD/outputs/swebench/my_batch/source" \
  python outputs/swebench/my_batch/source/experiments/run_swebench_mini_batch.py \
  --preflight-only --batch-dir outputs/swebench/my_batch

nohup python -u outputs/swebench/my_batch/source/experiments/run_swebench_mini_batch.py \
  --batch-dir outputs/swebench/my_batch \
  > outputs/swebench/my_batch/batch.log 2>&1 < /dev/null &
```

每个仓库域一个队列，域内逐题生成后立即官方评估。`status.json` 记录状态，`results.jsonl` 记录每题结果；原始轨迹和补丁位于 `instances/`。续跑同一冻结入口会跳过已结束任务，并保留未完成尝试。已有活动调度器时不要重复启动；文件锁及旧任务进程检测会拒绝重复运行。

## 检查与统计

```bash
python -m unittest discover -s test -p 'test_swebench_*.py' -q
# 需本地数据与镜像，模型响应被 mock，不消耗 API 额度。
python test/smoke_mini_worker_contract.py "$MINISWE_PYTHON"

# 统计需原始已完成批次；仓库中的派生 CSV/JSON 足以重绘图表。
python scripts/summarize_mini_batch.py outputs/swebench/mini_full_20260918_domains_v4b
python scripts/plot_mini_batch_report.py docs/experiments/mini_full_20260918
```

统计脚本用于本次 154 题实验的审计；修改数据集或实验规模时，应同步调整其中的完整性断言与报告模板，不能直接复用硬编码的实验结论。所有已生成原始批次与冻结源文件保持不变，发布整理不改变实验结果。
