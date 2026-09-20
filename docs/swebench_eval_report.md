# SWE-bench Verified Test Split 评测报告

**模型**：DeepSeek-V4-Flash-0731（GPTSwarm FullConnectedSwarm / SWEEditAgent）
**数据集**：`datasets/swebench/verified_new_split.json` test split，154 例（django 59 / sympy 39 / sphinx 33 / matplotlib 23）
**日期**：2026-09-16 ~ 2026-09-17

## 最终结果

| 指标 | 数量 | 占比 |
|---|---|---|
| **Resolved（通过）** | **45** | **29.2%** |
| Unresolved（patch 应用成功但测试未通过） | 84 | 54.5% |
| Empty patch（未能生成有效修改） | 25 | 16.2% |
| Error（patch 无法应用） | 0 | 0% |

分仓库 resolved 率：matplotlib **11/23 (47.8%)**、sympy 11/39 (28.2%)、django 15/59 (25.4%)、sphinx 8/33 (24.2%)。

合并报告：`outputs/swebench/report_combined_test154.json`（含 eval_004 对 2 个换行 bug 案例的重评）。

## 三轮评测漏斗

| 轮次 | 方法 | 输入实例 | 新增 resolved | 累计 resolved | 备注 |
|---|---|---|---|---|---|
| eval_001 | 原始 FullConnectedSwarm（LLM 手写 unified diff） | 154 | 15 | 15 (9.7%) | 128 例 patch 无法 apply |
| eval_002 | patch 语法修复 + hunk 重定位（不重新生成） | 128 | +5 | 20 (13.0%) | 42 例 patch 变为可 apply |
| eval_003 | **SWEEditAgent 重新生成**（读真实文件 + 编辑式修改） | 86 | +25 | **45 (29.2%)** | apply 错误率 83% → 2% |

## 失败原因分析（eval_001 的 128 个 error）

原始流程让 LLM 凭记忆直接手写 unified diff，产生四类问题：

1. **格式缺陷**：patch 末尾缺换行、hunk 头声明行数与正文不符、行号写成占位符 `@@ -XXX,6 +XXX,9 @@`；
2. **行号漂移**：hunk 起始行号与真实文件偏差几十行；
3. **上下文幻觉**：hunk 的上下文行是模型编造的，与真实文件内容不一致（轻则边缘几行，重则整段函数体不存在）；
4. **越权改测试**：模型修改测试文件，与官方 test patch 冲突。

根因：生成阶段 agent 从未读取仓库真实文件内容，diff 完全来自模型对代码库的记忆。

调试中发现的一个关键行为：**`git apply` 和 GNU patch 都会把"尾部没有上下文行的 hunk"强制锚定到文件末尾匹配**（对称地，头部无上下文锚定到文件开头）。修复工具必须为每个 hunk 补齐两侧各 3 行真实上下文（等价于 `git diff -U3` 的输出），否则字节级正确的 hunk 也会 apply 失败。

## 修复方案

### 第一层：patch 后处理（eval_002，救回 5 例）

- `scripts/fix_patches.py` — 语法规范化：重算 hunk 头行数、替换占位行号、hunk 内空行补上下文前缀、补末尾换行；
- `scripts/relocate_hunks.py` + `scripts/relocate_all.py` — 在每个实例的 docker 镜像内将 hunk 与真实源文件比对重定位：精确/去空白匹配 → 允许每侧裁掉至多 3 行编造的边缘上下文 → difflib 序列对齐兜底（过半行能对上即用真实文件内容重建 hunk），并补齐两侧真实上下文；同时剔除测试文件的修改。

结果：128 例中 42 例变为可 apply（APPLY_OK 8 + fuzz 可用 34），其余 86 例上下文幻觉过重，无法语法性救回。

### 第二层：生成流程重写（eval_003，救回 25 例）

新增 `SearchReplaceEdit` 操作（`swarm/environment/operations/swe_bench/code_edit.py`）与 `SWEEditAgent`（`swarm/environment/agents/swe_bench/code_agent.py`），流程：

1. LLM 从仓库文件列表 + issue 提及路径中选出至多 3 个候选文件；
2. 从容器读取**真实文件内容**（超长文件按 issue 关键词截取相关区域）；
3. LLM 输出 SEARCH/REPLACE 编辑块（要求逐字复制原文）；
4. 带空白容错地应用到容器内文件；失败的块附上"最接近的真实代码片段"再修一轮；
5. `py_compile` 语法校验，损坏的文件自动回滚；
6. `git diff` 从容器导出最终 patch —— **格式由构造保证合法**。

驱动脚本：`experiments/run_swebench_failed.py`（按报告 error_ids 重跑，支持断点续跑）；评测：`experiments/run_swebench_eval2.py` / `run_swebench_eval3.py`（须用 `.venv/bin/python`，swebench 2.1.6）。

结果：86 例全部完成生成，61 例产出合法 diff（其中 25 例 resolved），25 例 SEARCH 块两轮均未匹配上（记为 empty patch），仅 2 例 apply 错误。

## 执行时间

| 阶段 | 时间窗口 | 耗时 | 说明 |
|---|---|---|---|
| 原始生成（154 例，3-agent swarm + 投票） | 09-16 12:43 – 15:24 | **≈ 2 h 40 m** | 4 workers 按仓库分组 |
| eval_001 评测 | 09-17 01:01 – 01:50 | ≈ 49 m（纯跑测 15 m 46 s） | 含镜像/容器开销 |
| patch 语法修复 + 重定位（3 轮迭代） | 09-17 01:55 – 02:15 | ≈ 20 m | 128 容器 × 8 并发/轮 |
| eval_002 评测（128 例） | 09-17 02:15 – 02:32 | ≈ 17 m（纯跑测 14 m 49 s） | |
| SWEEditAgent 重新生成（86 例） | 09-17 02:55 – 04:42 | **≈ 1 h 47 m**（~75 s/例） | 每例 2-3 次 LLM 调用 |
| eval_003 评测（61 例非空） | 09-17 06:13 – 06:26 | ≈ 14 m（纯跑测 11 m 54 s） | |
| **合计** | | **≈ 6 h 07 m** | 生成 4.5 h + 评测 1.3 h + 修复 0.3 h |

## Token 消耗（估算）

⚠️ 现有日志未记录 API usage（`swarm/utils/globals.py` 的 `Cost` 单例未在运行结束时输出），以下为按 prompt 构成的估算，仅供量级参考：

| 阶段 | 估算输入 tokens | 估算输出 tokens | 依据 |
|---|---|---|---|
| SWEEditAgent 重跑（86 例） | ≈ 2.0 M | ≈ 0.3 M | 每例：选文件调用 ~4k + 编辑调用 10-25k（issue + ≤3 文件×≤30KB）+ 部分修复调用 ~8k；输出以编辑块为主（61 个 diff 合计 71 KB） |
| 原始生成（154 例，3 agent + 投票） | 无法可靠估算 | 无法可靠估算 | 多 agent 图各节点 prompt 未留存 |

**建议**：在 `swarm/llm/gpt_chat.py` 的 `agen` 中累计 `response.usage` 并在 runner 结束时落盘，下次运行即可得到精确数字。

## 产物清单

| 文件 | 内容 |
|---|---|
| `outputs/swebench/report_combined_test154.json` | 三轮合并最终报告 |
| `DeepSeek-V4-Flash-0731.eval_00{1,2,3}.json` | 各轮 harness 报告 |
| `outputs/swebench/predictions_harness.json` | 原始预测（154） |
| `outputs/swebench/predictions_harness_fixed.json` | 语法修复+重定位后的预测 |
| `outputs/swebench/predictions_retry.json` | SWEEditAgent 重新生成的预测（86） |
| `outputs/swebench/relocate_status.json` | 重定位逐例状态 |
| `logs/run_evaluation/eval_00{1,2,3}/` | harness 逐例日志 |

## 失败 case 轨迹分析（2026-09-17 补充）

对 eval_003 轮（SWEEditAgent 重跑的 86 例）的失败轨迹做了逐层归因。

### 结果分解与测试轨迹

34 个 unresolved 按 harness 测试结果分为三类：

| 类别 | 数量 | 说明 |
|---|---|---|
| 修复完全无效（FAIL_TO_PASS 0 通过） | 30 | patch 应用成功但目标测试全部仍失败 |
| 部分有效 | 1 | sphinx-11510：F2P 1 过 1 失败 |
| **修复成功但引入回归** | 3 | F2P 全过，仅打破 1-2 个既有测试，离通过一步之遥 |

3 个回归 case 及其打破的测试：`django-11490`（combinator 查询 1 个）、`django-11885`（large_delete 2 个）、`sphinx-8265`（AST unparse 1 个）——只要有 PASS_TO_PASS 测试反馈迭代一轮，大概率转为 resolved。

### 定位准确 ≠ 修复正确：瓶颈在修复内容

与 gold patch 的文件对比（eval_003 产出的 61 个 diff）：

| 组 | 命中全部 gold 源文件 | 部分命中 | 完全改错文件 |
|---|---|---|---|
| resolved (25) | 24 | 1 | 0 |
| unresolved (34) | 21 | 8 | **仅 5** |

文件定位准确率约 85%（至少部分命中），**失败主因不是找错地方，而是修复内容不足**：

- 模型 patch 与 gold patch 的大小比：resolved 中位数 **1.02**（和标准答案体量相当），unresolved 中位数 **0.62**，且 44% 不足 gold 的一半——典型的 under-fix；
- gold 修复复杂度：resolved 组中位数 1 个 hunk，unresolved/empty 组中位数 2 个 hunk——**多点修改的 issue 明显更难**；
- 典型样本：`django-11138` gold 需改 4 处函数（跨 mysql/sqlite/oracle 后端），模型只改了 1 处；`sympy-24443` 模型改了 `_image()`，gold 改的是同文件的 `homomorphism()`。

### 空 patch（25 例）：采样波动，重试可恢复

对 2 个空 patch case（`sympy-13031`、`matplotlib-25960`）做了带内部日志的重放探针：

- `sympy-13031`：重放时第一轮 1 个 SEARCH 块**直接命中并应用成功**——原次失败纯属 LLM 输出波动；
- `matplotlib-25960`：第一轮 4 个块全部未匹配，修复轮救回并成功编辑——一轮修复机会有时不够。

结论：空 patch 的主因是 **SEARCH 块逐字复制的保真度不稳定**（采样方差），而非能力缺失。佐证：25 例按仓库均匀分布（django 8 / sympy 7 / sphinx 6 / mpl 4），且只有 2/25 的 gold 修复涉及多文件。预计整体重试 1-2 次可恢复大部分生成，按 29% 的通过率折算约可再多 5-7 个 resolved。

### 2 个 apply error：postprocess 换行 bug（已修复）

`SWEBenchDataset.postprocess_answer()` 的 `.strip()` 会剥掉 git diff 的末尾换行。59 个 patch 因最后一行是上下文行被 GNU patch 容忍；`django-11400`、`sympy-20590` 最后一行恰好是 `+` 行，触发 "patch unexpectedly ends in middle of line" 拒绝。已修复（`swe_bench_dataset.py` 对 diff 输出补回换行），eval_004 重评后 2 例均正常 apply（转 unresolved），**error 清零**。

最终合并（含 eval_004）：**resolved 45 / unresolved 84 / empty 25 / error 0**。

### 由轨迹推导的改进优先级

1. **空 patch 重试**（预期收益最高，~5-7 例）：生成为空时自动重跑 1-2 次，或增加修复轮数；
2. **测试反馈闭环**（3 个回归 + 部分 under-fix）：容器内已具备跑测能力，patch 后跑 FAIL_TO_PASS/PASS_TO_PASS 并把失败输出喂回模型迭代；
3. **对抗 under-fix**：提示模型显式列出 issue 的全部触发路径/需要修改的所有位置，放宽单文件 30KB 与 3 文件上限；
4. **定位纠偏**（5 例改错文件）：定位阶段附上 grep 关键符号的证据链再让模型选文件。
