# 最新 SWE 基线实验

新实验入口：[断网基线快速启动指南](../SWE_OFFLINE_QUICKSTART.md)。

整理日期：2026-09-28。本次补充精简审计数据并清理旧运行，没有重跑或改动评分。

| 数据集 | 有效结果 | 报告目录 | 本地原始批次 |
|---|---:|---|---|
| Verified test 子集 | 102/154，66.23% | [mini_full_20260918](mini_full_20260918/) | `mini_full_20260918_domains_v4b` |
| Pro test 子集 | 100/216，46.30% | [mini_pro_20260920](mini_pro_20260920/) | `mini_pro_full_20260920_domains_v2` + `mini_pro_retry_20260921_budget_v1` |

Pro 按原报告的额度异常替换规则选择尝试，原始批次和补跑批次共同组成最终结果；不是按评估成绩取最优。选择清单与被替换尝试的费用统计继续保留在原报告目录。

每份报告新增 `audit/`：

- `predictions.jsonl`：与报告逐题清单一致的模型补丁，仅包含评估所需三字段。
- `evaluations.jsonl`：逐题评分和可用的详细测试结果；缺失详细报告时明确为 `null`。
- `selection.json`：选中尝试的原始目录、评分、补丁大小和 SHA-256。
- `batches/`：批次参数、manifest 和运行时源码快照。快照用于复核，当前可执行代码仍在正常源码目录。
- `export_manifest.json`：导出文件校验值与原报告逐题表校验值。

三批完整轨迹、API usage、评估日志和输入文件仍在本机 `outputs/swebench/`，由 Git 忽略。依赖环境、数据缓存和本地 `.env` 保留。旧冒烟、重复启动、失效实验及根目录旧日志已归档至：

`/home/xhgong/project_cleanup_archive/20260928_swe_baselines/GPTSwarm/`

父目录 `manifest.json` 记录归档前的相对路径、文件数、大小及原因。历史诊断文档中的旧运行路径可在归档中找到。恢复时复制回原路径，避免覆盖现有文件。

运行方式与迁移经验见 [mini 固定协作基线](../swebench_mini_fixed_baseline.md)、[Pro 适配](../swebench_pro.md)、[跨框架迁移经验](../swe_cross_framework_migration_playbook.md)。本次仍保留固定 Swarm 多角色协作，不进行边优化。
