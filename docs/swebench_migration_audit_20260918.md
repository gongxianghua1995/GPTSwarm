# GPTSwarm SWE 接入审计（2026-09-18）

本文件记录变更前审计。后续已按用户确定的多角色固定协作图实施，见 [GPTSwarm + mini 固定基线](swebench_mini_fixed_baseline.md)。

基于本地代码、已有 harness 报告、154 条 repair 轨迹、最新三 agent 轨迹，以及 EvoMAS / MetaGPT 接入经验。此次只增加审计文档，没有修改运行代码或启动新的模型实验。

## 结论与实验口径

目前证据支持：执行器的定位与返工能力、候选选择机制、实验可观测性存在缺口；不足以判定 GPTSwarm 框架本身无效。当前单 agent 已有一定修复能力，不能把所有失败归为“本地 agent 太弱”。

| 实验 | 实际含义 | 已有结果 |
|---|---|---|
| `eval_001` | 旧版混合 agent，模型手写 diff | 15/154 resolved，128 apply error |
| `report_combined_test154.json` | 原始结果、补丁修复、失败题重新生成的合并结果 | 45/154（29.2%），不能当成一次冻结的团队运行 |
| `eval_repair_001` | `run_swebench_repair.py` 逐题直接调用一个 `SWERepairAgent` | 64/154（41.6%），70 unresolved，20 empty，0 error |
| `native_capable_a3_20260917_141244_600644` | 三个相同 `SWECapableAgent` 独立工作，最终投票 | 1 题，0 resolved，非空且可评估 |

来源：根目录对应 harness JSON、`outputs/swebench/report_combined_test154.json`、`experiments/run_swebench_repair.py:73`。repair 预测和报告的 154 个 ID、134 个非空数量一致；其 runner 会重试已有空预测，现有记录不足以证明每题只有一次历史尝试。文档 `swebench_eval_report.md` 的“最终 45/154”已不能代表所有现有实验。

这些结果来自 SWE-bench Verified 的四个 Python 仓库，不能直接与 MetaGPT 的 SWE-bench Pro 四域结果比较。最新 native 三 agent 只完成一个样本，未找到对应 `native_capable_a1_*` 结果，不能据此估计团队收益。

## 1. 定位失败后仍继续编辑，返工缺少读文件能力

当前 native worker 是固定流程：定位最多 3 个现有 Python 文件，每文件最多 30,000 字符，最多 2 次初始编辑尝试，再进行最多 3 轮验证。它不是可以自主执行“搜索—读文件—编辑—运行测试—重新定位”的工具循环。

- `_locate_files` 输出预算仅 400 tokens，异常直接变成空列表；没有可靠的定位失败恢复路径（`code_edit.py:194`）。
- 初始编辑失败时有真实邻近代码补充，但验证后的返工只看到截断的 diff 和测试输出，并被要求从 diff 上下文复制 SEARCH 文本；没有重新获取相关源码的模型工具（`test_feedback.py:255`）。
- 返工没有任何块应用成功就立即结束，不会用剩余轮次重新定位或获取精确上下文（同文件 `:284`）。
- 现有文件定位只列出 `.py`，导出使用 `git diff HEAD`，不包含未跟踪新文件。适用范围限制是明示设计，但会限制复杂任务与跨语言迁移。

对 `predictions_repair_traces` 的全部 154 条轨迹按 `eval_repair_001` 分组统计：

| 轨迹现象 | 数量 | 解释 |
|---|---|---|
| 20 个 empty 中出现 `files selected: []` | 15 | 没选中文件后仍走编辑流程；不是 15 个都能单因归结为定位 |
| 20 个 empty 中记录语法检查后回滚 | 2 | 日志未保留完整编译诊断，不宜直接断言均为源码语法错误 |
| 70 个 unresolved 中出现 `applied repair edits to []` | 24 | 返工执行没有落地 |
| 70 个 unresolved 中出现 `no actionable repair output` | 9 | 返工输出无法执行；与其他统计可重叠 |
| 70 个 unresolved 中曾出现 `repro_ok=True` | 23 | 自写复现通过不等于官方评估通过 |

例：`django__django-11239` 未选中文件，连续编辑和修复均未落地，最后输出空补丁。统计是日志现象，不是互斥根因，也不能证明 400-token 截断实际发生。

## 2. 最新三 agent 的选择器丢弃了验证证据

`WorkspaceDiff` 的 `valid` 只检查非空 diff、测试路径限制、Python 编译和反向 apply；没有表达修复正确性，也没有把 `LocalVerification.checks` 传给最终选择器（`native_agent.py:107`）。

`NativePatchVote` 对完整 patch 字符串计票，平票用固定种子随机选择（同文件 `:168`）。在最新 `django__django-10999` 轨迹中：

| Agent 索引 | 最后一次自写复现 | 5 个回归测试 | 格式有效 | 最终选中 |
|---|---|---|---|---|
| 0 | 失败 | 通过 | 是 | 否 |
| 1 | 失败 | 通过 | 是 | 是 |
| 2 | 通过 | 通过 | 是 | 否 |

三个 patch 均不同，因此 3 候选、3 种补丁、3 方平票。Agent 1 的日志明确指出负天数被解析成正天数，随后返工 `applied repair edits to []`，仍被选中；官方 eval 判为 unresolved。整个生成过程耗时 1741.96 秒，约 29 分钟。

这是选择器没有利用已有证据的直接例子。但三个 agent 的自写复现不同，Agent 2 的检查也更窄，不能推出“选 Agent 2 就能通过官方评估”。适合增加统一的公开行为检查、证据传递和候选复核；不应简单把自写复现失败作为绝对淘汰条件。

证据目录：`outputs/swebench/native_capable_a3_20260917_141244_600644/` 的 `trace.json`、`candidates.json`、`config.json`、`report.json`。

## 3. 当前 native 图验证的是独立候选集成

三个 agent 实现相同、各有独立容器，内部节点为 `WorkspaceInput → SourceEdit → LocalVerification → WorkspaceDiff`。分支之间没有通信，只连向最终投票；没有角色交接、联合返工或拓扑优化（`run_swebench_native_smoke.py:17`、`:44`）。

这可以作为固定图集成基线，但不能单独证明“协作机制有效”。`Graph.run` 逐节点 await，三分支实际顺序执行，不是同时运行（`swarm/graph/graph.py:133`）。三 agent 的总调用额度更大；同能力单 agent 对照和相同总预算对照应分别报告。

## 4. 测试不可用、预算耗尽和模型失败没有充分区分

- 回归命令异常或返回码不是 0/1 时返回空字符串；`_regressions_ok('')` 为 True。native 虽记录 `unavailable`，内部仍可能把无可用回归检查当成满足停止条件（`test_feedback.py:179`）。应保留 passed / failed / unavailable 三态及实际测试数量。
- 定位 400、复现 2000、编辑/返工 8192 tokens；尚未形成按阶段独立配置的预算接口。
- `gpt_chat.py:90` 单次请求外层超时 1000 秒，外层重试最多 10 次；图的 2400 秒限制作用于每个节点，不是每题总时限。
- LLM 接口主要向上返回 `message.content`，此次 runner 没有逐请求落盘 finish_reason、reasoning token 明细和准确 usage，因此无法确认“空响应是思考吃满输出预算”这一假设。
- 同步 `subprocess.run` 位于异步节点中，会阻塞事件循环；主机侧 docker exec 超时不足以保证容器内子进程已终止。这是静态实现风险，本次未复现残留进程。

迁移 MetaGPT 经验时，应同时记录实际请求参数、响应终止原因、预算与耗时；增加任务总 deadline、容器内命令超时和取消后的清理。不能只扩大某一个 max_tokens 或节点 timeout。

## 5. 断网与 Git 隔离尚未闭环

生成容器默认 `--network none`，这一点已经实现。当前 native runner 也在生成前删除了 gold `patch` 和 `test_patch`，并为候选提供独立容器。

但还有三个缺口：

1. native eval 复用的 hook 只修改镜像构建。当前本地 swebench 的 `docker_build.build_container` 调用 `client.containers.create` 时没有 `network_mode='none'` 或 `network_disabled`。因此不能把生成配置的 `network='none'` 当成 eval 也断网的证据。应在 eval 创建路径显式设置并 inspect 确认；本次没有启动容器验证历史运行的实际网络模式。
2. native 生成启动路径没有校验 HEAD 与任务 base commit、一开始的 dirty tree，也没有清理未来 Git refs/objects。断网不自动消除镜像内 Git 历史带来的答案访问风险；是否实际包含未来修复需另查镜像，不应直接宣称发生了泄漏。
3. 补丁依赖图正常运行到导出节点；runner 遇到缺失节点输出就抛错，finally 销毁容器。缺少统一的异常/超时后独立回收补丁机制和初始工作区差异过滤；新增文件也未导出。

源码位置：`env.py:56`、`:105`；`run_swebench_native_smoke.py:61`、`:75`、`:90`、`:101`；本地 `.venv/lib/python3.14/site-packages/swebench/harness/docker_build.py:520`。

## 6. 多域 Pro 迁移不能只换数据文件

当前 native 入口固定读取 Verified154，镜像名按 Verified 规则拼接，工作区固定 `/testbed`，源码检索限定 Python，验证假设 conda `testbed` 和 Python 测试。这不是已经完成的 Pro 多域接入。

若迁移到与 MetaGPT 相同的 Pro 协议，应按实例读取镜像/工作区信息，处理镜像 entrypoint、语言与测试命令差异、BusyBox timeout、子模块和补丁导出；统一 hints / 测试名称可见性。当前 native 向单 agent 与团队同样提供 PASS_TO_PASS 名称，但跨框架对照也必须保持一致。

## 建议实施顺序

1. **固定实验口径和可观测性。** 分开旧混合流程、单 repair、native 集成结果；落盘实际配置、源码版本、逐调用 usage/终止原因、独立错误类型、生成与 eval 网络检查。空补丁不能兼任所有环境或请求错误的标签。
2. **优先替换执行器，保留 GPTSwarm 图。** 在每个私有容器内接入真正的 mini-swe-agent 工具循环，支持读、搜、编辑、测试和再次修复；由宿主独立导出工作区 patch。先保持三独立候选加原投票，单独测执行器替换的收益，不直接移植 MetaGPT 角色体系冒充 GPTSwarm。
3. **单独实验改进选择器。** 保存每个候选的检查状态、命令、退出码和对应工作区版本，用统一检查和复核辅助选择；原投票与新选择器分别报告，避免同时改多个因素。
4. **再验证协作图。** 如果目标是通信或拓扑收益，增加明确消息边和返工协议，保留独立集成对照；运行同能力单 mini、固定图多 mini，并做总预算匹配。先通过小样本端到端检查，再全量多域并行。

mini 的价值是提供成熟的执行循环，不保证解决率必然提高。模型、数据集、预算、输入信息和图都需要控制，才能把变化归因到执行器或协作机制。

## 本次验证范围

执行 `.venv/bin/python -m unittest discover -s test -p test_swebench_native.py -q`，现有 6 项测试全部通过。这些测试验证容器绑定、图结构、投票及复现标记等接入行为，不验证实际 SWE 修复成功率。本次没有新增测试、发起模型调用或重跑官方评估。
