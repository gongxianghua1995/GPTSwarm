# GPTSwarm：SWE 断网基线快速启动

更新：2026-09-30。面向接手实验的同事；先做零模型调用检查，再按需启动真实实验。

## GPTSwarm 的环境、固定协作图与续跑

框架入口 `experiments/run_swebench_mini_batch.py` 和 `run_swebench_mini_swarm.py` 已上传。新入口仍调用它们，保留 Analyst → Engineer → Reviewer → Engineer 返工 → Reviewer 复核的固定图；不做边优化，不采用独立重复作答后随机选答案。

```bash
# 新机器先按根目录 README / pyproject.toml 安装主环境，推荐单独 Python 3.11 环境。
# python3.11 -m venv .venv
# .venv/bin/python -m pip install -e .
# mini/eval 单独环境，参考版本如下：
# python3.11 -m venv .venv-mini
# .venv-mini/bin/python -m pip install 'mini-swe-agent==2.4.6' 'swebench==5.0.2'
export SWE_PYTHON=/path/to/gptswarm-env/bin/python
export MINISWE_PYTHON=/path/to/mini-eval-env/bin/python
export SWE_EVAL_PYTHON="$MINISWE_PYTHON"
```

根目录 `.env` 配置 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`（或 `OPENAI_API_BASE`）。原实验 mini 安装包含本地适配，版本号相同不代表源码完全相同；严格复现请取得已验证的环境/源码快照。这里验证了已有环境的调用链，没有测试在空白机器上安装完整依赖。

输入包还原到 `outputs/swebench/swebench_verified_test_154.json` 和 `outputs/swebench/pro_test_216.json`；也可 `--data /path/to/full.json` 指定同 schema 的完整任务输入。Pro 的原始 Arrow 转换入口仍是 `scripts/prepare_swebench_pro.py`，其划分必须与已提交 test ID 一致。

Verified 配置 `config/swebench/mini_fixed.json`，Pro 配置 `config/swebench/mini_pro_fixed.json`；可通过 `--config` 指定复制后的配置。默认每题生成总时间 1200 秒、各阶段单次输出 16384 tokens，eval 单独计时。修改模型名直接修改所用 JSON 的 `model`。

`run` 首先执行原入口 `--prepare-only`，冻结源码、实际安装的 mini、输入、配置、镜像 ID，再运行冻结目录中的控制器。每个 repository 一条顺序队列，四域并行；这里的 `--workers` 不改变固定域队列数。新入口会为新批次启用已有的额度耗尽暂停机制。

```bash
# 相同输入、限额和输出目录恢复，已完成任务跳过；中断尝试保留。
python3 scripts/swe_offline.py resume --benchmark pro --output outputs/swebench/pro_new
# 如果最初使用了 --limit-per-domain/--domain/--data，恢复时也带上相同参数。
```

恢复使用该目录冻结的配置和源码，不会把当前代码修改注入旧实验。输入选择变化会被拒绝。额度已恢复并不自动把历史 finished 空补丁重做；需要按明确额度错误规则建立独立补跑批次，参见 [Pro 适配与补跑说明](swebench_pro.md)。

进度看 `<output>/status.json`，逐次结果看 `results.jsonl`，轨迹/补丁看 `instances/`，评估日志位于批次 `source/logs/`，预算与镜像溯源看 `manifest.json`。容器级无模型预检可在准备完成后额外调用冻结入口的 `--preflight-only`；它会创建容器，不属于本次零容器启动检查。

最新成绩与选择口径见 [实验索引](experiments/README.md)，角色和预算实现见 [固定基线说明](swebench_mini_fixed_baseline.md)。


## 1. 先准备环境、输入和镜像

工作分支是 `swe-baselines-offline-20260928`。下面所有命令在仓库根目录执行；Linux / x86_64，Docker daemon 可访问。模型 API 由宿主机调用，生成和评估容器断网，宿主机仍需要访问 API。

首次获取（从项目的父目录执行）；已有此分支的工作副本直接 `git pull --ff-only`：

```bash
git clone --single-branch --branch swe-baselines-offline-20260928 https://gitcode.com/gxh_1995/GPTSwarm.git
cd GPTSwarm
```

不要把旧报告的 `predictions.jsonl` 当作任务输入。已提交 `scripts/swe_offline_ids/{verified,pro}.txt` 固定 test 子集：Verified 154 条（59/23/33/39），Pro 216 条（63/54/60/39）。脚本按 ID 选择，缺题或重复 ID 直接失败。

配套输入包已随 Git 提交到 `scripts/swe_offline_ids/gptswarm-swe-inputs.tar.gz`，普通 clone/pull 即可取得，不使用 Git LFS，也无需另外下载或手动解压。使用默认输入路径执行下文的 `check` / `run` / `resume` 时，启动器会自动补齐缺失输入，校验压缩包和文件的 SHA-256；校验失败会停止，已有内容不同的文件不会被覆盖。显式 `--data` 指向其他位置时，仍使用你提供的输入，不自动还原默认数据。

包内包含任务描述、仓库/基础提交、参考补丁和测试元数据，**不含历史模型补丁、轨迹或 API 配置，也不含 Docker 镜像或 Python 环境**。文件列表、大小和 SHA-256 见同目录 `input_manifest.json`。EvoMAS 包保留较大的源数据集合，实际实验仍按固定 ID 选择 Verified 154 条 / Pro 216 条；GPTSwarm、MetaGPT 包直接包含这两个子集。

在已准备好 Python 环境、API 配置和本地镜像的服务器上，拉取此分支后按下文先 `check` 再 `run`，无需维护者另发数据包。新服务器仍须先准备环境和镜像。gold patch、测试补丁和隐藏评估信息只用于宿主评估路径；EvoMAS 历史基线的 F2P 提示注入例外见下文。

Docker 镜像需要提前取得。`check` 只读本地镜像列表，缺镜像会返回非零；不会联网拉取、构建或启动容器。在实验机可以 `docker save -o swe-images.tar <所需镜像标签...>`，在新机器 `docker load -i swe-images.tar`。Verified 生成标签为 `swebench/sweb.eval.x86_64.<instance_id 的 __ 替换成 _1776_>:latest`；Pro 为 `jefzda/sweap-images:<dockerhub_tag 前 128 字符>`。GPTSwarm Verified 还需同内容的 `sweb.eval.x86_64.<原 instance_id>:latest` 评估标签。镜像及其环境改动也属于复现条件；不能把任意同名镜像视为等价。

## 2. 无模型调用的启动检查

`scripts/swe_offline.py` 仅依赖标准库，可用 `python3` 驱动；实际框架、mini 和 eval 的 Python 可分别通过 `SWE_PYTHON`、`MINISWE_PYTHON`、`SWE_EVAL_PYTHON` 或同名 CLI 参数指定。后两项默认继承前一项，MetaGPT/GPTSwarm 请按下面的双环境示例设置。

```bash
python3 scripts/swe_offline.py --help
python3 scripts/swe_offline.py check --benchmark verified \
  --output outputs/swebench/verified_check --limit-per-domain 1
python3 scripts/swe_offline.py check --benchmark pro \
  --output outputs/swebench/pro_check --limit-per-domain 1
```

检查会加载真实入口及依赖、用真实 argparse 校验启动参数（解析后立即退出）、校验任务 ID/必要字段/配置，以及只读检查 Docker 镜像。检查子进程禁止 socket 连接。**不会调用模型、创建容器、运行测试集或创建实验输出目录**；首次使用默认路径会解压缺失输入，框架导入可能写自身日志/缓存。首次导入依赖可能需要几分钟，不是 SWE 任务的生成时限。

成功返回码为 0，JSON 含 `model_calls: 0`、`containers_started: 0`。可加 `--report /tmp/check.json` 保存检查结果。删掉 `--limit-per-domain 1` 可检查完整 154/216 条输入及镜像；`--domain django` / `--domain ansible` 只选一个域。`--limit-per-domain` 对每个选中域分别限额。

这层检查不验证 API 额度、模型解题能力、容器内部工具链或测试结果。新镜像仍应在正式计分前单独做容器预检及评估器对照，不能用这里的“通过”代替真实评估通过率。

## 3. 启动实验

先确认 API 配置和模型名称，再使用新目录。以下 `run` 才会真正调用 API 并产生费用；本指南提交前未执行这些命令。

```bash
# 小规模真实运行：每域一题。也可加 --domain 只跑一个域。
python3 scripts/swe_offline.py run --benchmark pro \
  --output outputs/swebench/pro_first4 --limit-per-domain 1

# 全量：去掉限额。Verified 154 条，Pro 216 条。
python3 scripts/swe_offline.py run --benchmark verified --output outputs/swebench/verified_new
python3 scripts/swe_offline.py run --benchmark pro --output outputs/swebench/pro_new
```

新运行拒绝覆盖已存在的目录，目录 basename 只用字母、数字、点、横线、下划线。可用 `tmux` 保持前台进程，或在启动命令外层使用 `nohup` 并把 stdout/stderr 重定向到独立日志。启动前同样会执行 `check`。

## 4. 故障处理与结果口径

- `Input lacks ... IDs`：输入包版本不对或给了局部输入；拿完整输入，再通过 `--domain` / `--limit-per-domain` 选择子集。
- 依赖/入口检查失败：在指定 Python 下执行相应入口的 `--help` 定位缺包，检查主环境与 mini/eval 环境是否混用。检查模式不会回显提供商配置文本。
- 镜像缺失：先取得镜像再运行，不在正式生成期间联网安装依赖，也不要删除缺镜像的任务改变分母。
- 401/402/额度不足：检查宿主 API 账户和配置，不把额度造成的空补丁解释为框架能力；续跑规则见项目说明。
- 非空 patch 不等于 resolved；最终评分来自对应任务的独立评估。基础设施失败、缺测试和模型未解决要分开保留。
- 记录输入/config/source hash、每次尝试、API usage 和起止时间；重跑保留原始尝试，不按评估结果挑最好答案。EvoMAS 原报告的合并口径属于历史例外，不能用于新实验的单次成绩。

压缩输入包随 Git 分发，解压数据沿用各项目原有跟踪/忽略规则；运行结果不提交，仅保留精简报告和必要审计材料。不得提交 `.env` 或 `config/config2.yaml`。

## 本次提交前的验证

Verified 154 条和 Pro 216 条的完整输入/CLI/本地镜像检查均通过，模型调用和容器启动均为 0。启动器相关回归测试 7 项通过。另外对两个数据集各四题实际执行了 prepare-only，并解析冻结后的入口，没有启动任务。详情与源码校验值见 [冒烟记录](SWE_OFFLINE_SMOKE_20260929.json)。

2026-09-30 输入包随仓库分发：空目录自动恢复、重复恢复不改写、三个项目各自 Verified 154 / Pro 216 全量启动检查通过；未调用模型或启动容器。详见 [输入包冒烟记录](SWE_INPUT_BUNDLE_SMOKE_20260930.json)。
