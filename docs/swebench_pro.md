# GPTSwarm + mini：SWE-bench Pro test

使用既有 `swebench_pro_split.json` 的 `families.*.test`，共 216 条：Ansible 63、Flipt 54、OpenLibrary 60、Webclients 39。它是本地实验划分的 test 子集，不是完整公开 Pro 数据集。smoke/opt 样例不参与本批；不复用其他框架的预测。

## 团队和预算

保持原生 Swarm 固定图：Analyst → Engineer → Reviewer → Engineer 返工 → Reviewer 复核。三个逻辑角色、不同提示词，均使用 mini-swe-agent；不优化边或节点。角色协议和共享预算沿用 Verified 实验。

配置为 [`mini_pro_fixed.json`](../config/swebench/mini_pro_fixed.json)：DeepSeek-V4-Flash-0731、temperature 0.2、每题共享生成时间 1,200 秒，单次输出上限 16,384 tokens、API timeout 300 秒。评估单独计时，上限 1,800 秒；批处理每题进程保护上限 3,600 秒。每个域一个队列，域内顺序生成、评估，四域并行。

## Pro 适配

- 从本机缓存的原始 ScaleAI Pro Arrow 提取既有 test ID，记录原始数据、划分和输出 SHA-256。模型输入包含题目及公开 `requirements`/`interface`；gold patch、test patch、F2P/P2P 名称和隐藏测试准备命令仅在宿主评估端使用。
- 复用本机 `jefzda/sweap-images` 镜像，tag 截断至 128 字符，代码目录为 `/app`。保留镜像 PATH，避免 login shell 丢失 Go 工具链；Ansible 使用 `PYTHONPATH=/app/lib`。
- 镜像安装过程可能修改 `yarn.lock`、`package-lock.json`、`requirements.txt`，Flipt 还会更新 `go.work.sum`。OpenLibrary 个别镜像另有 Selenium/PyYAML 测试初始化兼容改动，Webclients 个别镜像重新构建了两份 `public/assets/sandbox.js`。生成前只还原这些已知文件至基线，保留已安装依赖；其他未提交源码修改触发预检失败。
- 保留 OpenLibrary 已初始化子模块和导入链接，将主仓库及子模块都转换为只含当前基线提交的浅仓库，删除历史 refs/对象。生成容器不执行隐藏测试 checkout。
- 增加 Go `_test.go` 和 Jest `.test/.spec` 文件识别，提交补丁排除测试文件；执行证据识别 Jest 及“未找到测试”。
- 所有生成、基线检查、评审及评估容器使用 `network=none`，启动后检查网络配置并记录。模型 API 由宿主调用。

## 评估协议

Pro 评估入口是 [`swebench_mini_pro_eval.py`](../experiments/swebench_mini_pro_eval.py)，在独立的 mini/eval Python 环境中运行 swebench 5.x；本机为 Python 3.11、mini-swe-agent 2.4.6、swebench 5.0.2。Verified 主环境继续使用原有评估依赖。

Pro TestSpec 和 Python/Go/Jest 日志解析沿用本机 MetaGPT Pro 适配协议。提交补丁先应用到评估镜像，再仅恢复数据集指定的测试文件；剔除原始准备命令中的整仓 reset/clean/checkout，避免擦除提交的修改。隐藏测试准备失败立即中止。Jest 使用已安装的本地工具、单进程运行；缺失测试不能视为通过。此入口是 SWE-bench harness 加 Pro 适配，并非 stock Verified 评估器。

正式启动前用每域一题做两种独立的评估器对照：标准补丁应通过，不修改源码的补丁应失败。对照只验证评估通路，不参与模型成绩，也不向模型提供测试反馈。另对 216 个生成镜像逐一检查基线、历史隔离、断网和初始空补丁；预检不调用模型。

## 运行

先准备数据（输出路径必须不存在）：

```bash
python scripts/prepare_swebench_pro.py \
  --source-arrow /path/to/swe-bench_pro-test.arrow \
  --split-json datasets/swebench/swebench_pro_split.json \
  --output outputs/swebench/pro_test_216.json

python -m experiments.run_swebench_mini_batch \
  --prepare-only --batch-dir outputs/swebench/my_pro_batch \
  --data-path outputs/swebench/pro_test_216.json \
  --config config/swebench/mini_pro_fixed.json \
  --mini-python "$MINISWE_PYTHON"

python outputs/swebench/my_pro_batch/source/experiments/run_swebench_mini_batch.py \
  --preflight-only --batch-dir outputs/swebench/my_pro_batch

nohup python -u outputs/swebench/my_pro_batch/source/experiments/run_swebench_mini_batch.py \
  --batch-dir outputs/swebench/my_pro_batch \
  > outputs/swebench/my_pro_batch/batch.log 2>&1 < /dev/null &
```

配置 `MINISWE_PYTHON` 指向同时安装 mini-swe-agent 与 swebench 5.x 的环境。冻结批次记录代码/数据/config 哈希、镜像 ID 和 worker 依赖版本。`status.json`、`results.jsonl` 和 `instances/` 保留进展、结果、时间与 API usage；Pro 评估详情位于单题的 `eval_details.json`，原始 harness 日志保留在冻结源码目录的 `logs/`。不要直接使用含 154 题断言的旧 Verified 报告脚本汇总 Pro。

## 2026-09-20 启动记录

正式批次为 `outputs/swebench/mini_pro_full_20260920_domains_v2`，于 2026-09-20 04:21:06 UTC 启动。216/216 镜像预检通过（首轮发现的 42 个镜像构建差异均经定向复检）；四域标准补丁和基线对照共 8/8 符合预期，38 项单元测试及 Go 镜像中的实际 mini worker 模拟模型协议测试通过。早期冻结 v1 未启动模型调用，保留 `ABORTED.json` 说明。

启动检查确认四个域都已收到真实模型响应，8 个初始工作容器均断网。API 出现过 RateLimitError，已有重试后返回响应的记录；当时尚无完成题目，此处不报告解决率。实时状态见批次的 `status.json`，启动审计见 `launch_audit.json`，后续统计以 `results.jsonl` 和完整轨迹为准。

## 2026-09-21 API 预算异常补跑

原批次后续出现明确的 `Budget has been exceeded`：89 条受影响，其中 85 条空补丁、4 条未解决。普通限流后成功重试的用例不因该原因入选。本次补跑于 2026-09-21 01:26:59 UTC 启动，目录为 `outputs/swebench/mini_pro_retry_20260921_budget_v1`：Ansible 27、Flipt 24、OpenLibrary 27、Webclients 11。

补跑直接复制原批次冻结源码及配置，保留相同的 agent、提示词、模型、预算与评估器，不向模型提供上次轨迹、补丁或评估反馈。89 个镜像 ID 和数据记录与原批次一致，复用对应预检证据。只有调度器新增两项保护：Ansible 域等待原队列收尾；检测明确的 API 预算耗尽后中断当前尝试、暂停整批，保留待跑任务。普通瞬时限流仍按原实现重试。保护逻辑通过 5 项测试，包含实际调度循环在预算错误下停止启动后续任务的模拟验证。

新批次 `selection.json` 保存选择依据，`manifest.json` 保存与原批次的关联和源码差异。原始结果不覆盖。合并结果时，对全部 89 个选中 ID 使用完成的补跑结果，无论成绩升降；补跑被中断的 ID 仍视为待补跑，不取两次中的最好补丁。

若新批次因预算暂停，`status.json` 会记录 `pause_reason.reason=api_budget_exhausted`。预算恢复后，用该批次的冻结入口和相同 `--batch-dir` 续跑；已结束任务跳过，中断任务创建新的尝试目录。
