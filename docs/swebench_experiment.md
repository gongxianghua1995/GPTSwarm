# SWE-bench Benchmark 实验纪要

## 实验目的

在 GPTSwarm 框架上集成 SWE-bench 基准测试，实现**容器内代码修复任务**的自动化评估。

**核心特点**: Agent 在容器内直接读取和修改代码，不依赖外部网络。

---

## 项目结构

### 1. 新增文件清单

#### 数据集模块
```
experiments/evaluator/datasets/swe_bench_dataset.py
```

#### Domain 模块
```
swarm/environment/domain/swe_bench/
├── __init__.py
├── env.py          # 环境管理 + RepoLock
├── evaluator.py    # 评估器
└── parser.py       # Patch 解析器
```

#### Agent 模块
```
swarm/environment/agents/swe_bench/
├── __init__.py
├── code_io.py         # SWECodeIOAgent (直接生成 patch)
├── code_react.py       # SWEReActAgent (ReAct 风格)
└── code_agent.py       # SWECodeAgent, SWEReActCodeAgent, SWEMultiStepAgent
```

#### Operations 模块 (容器内交互)
```
swarm/environment/operations/swe_bench/
├── __init__.py
├── direct_answer.py    # 直接输出 patch
├── file_analyse.py    # 分析文件
├── file_ops.py        # 容器内文件操作 (FileRead, FileWrite, BashCommand, GrepSearch, GitDiff)
└── web_search.py       # Web 搜索
```

#### Prompt 模块
```
swarm/environment/prompt/swe_bench_prompt_set.py
```

#### 运行脚本
```
experiments/run_swebench.py
experiments/smoke_test.py
```

#### 数据文件
```
datasets/swebench/
├── smoke_test.json           # 单个冒烟测试用例
├── verified_146_split.json   # 146 个测试用例
├── verified_new_split.json   # 新验证集 (154 test + 90 opt)
├── verified_new_full.json    # 完整实例数据 (断网可用)
└── swebench_pro_split.json   # Pro 版本
```

### 2. 修改文件

- `swarm/environment/agents/__init__.py` - 添加 SWE agents 导出
- `swarm/environment/agents/swe_bench/__init__.py` - 添加新 agents
- `swarm/environment/operations/__init__.py` - 添加 SWE operations 导出
- `swarm/environment/operations/swe_bench/__init__.py` - 添加新 operations
- `experiments/evaluator/datasets/swe_bench_dataset.py` - 支持断网本地数据加载
- `experiments/run_swebench.py` - 支持多种 agent 类型

---

## Agent 配置

### 1. SWECodeIOAgent (纯基线)

LLM 直接根据问题描述生成 patch，**不看代码**。

```python
from swarm.environment.agents.swe_bench.code_io import SWECodeIOAgent

agent = SWECodeIOAgent(domain='swe_bench', model_name='gpt-4')
```

**适用场景**: 基线测试，评估 LLM 直接生成 patch 的能力。

### 2. SWECodeAgent (推荐 - 容器内分析)

Agent 在容器内读取代码文件，然后生成 patch。

```python
from swarm.environment.agents.swe_bench.code_agent import SWECodeAgent

agent = SWECodeAgent(
    domain='swe_bench',
    model_name='gpt-4',
    files_to_read=['src/main.py'],  # 可选：指定要读取的文件
)
```

**工作流程**:
1. `FileRead`: 读取仓库文件
2. `DirectAnswer`: 分析代码，生成 patch
3. `GitDiff`: 从 git diff 提取最终 patch

### 3. SWEReActAgent

使用多步推理的 Agent。

```python
from swarm.environment.agents.swe_bench.code_react import SWEReActAgent

agent = SWEReActAgent(
    domain='swe_bench',
    model_name='gpt-4',
    num_reasoning_steps=3
)
```

### 4. SWEMultiStepAgent

多步骤 agent，适合复杂 bug。

```python
from swarm.environment.agents.swe_bench.code_agent import SWEMultiStepAgent

agent = SWEMultiStepAgent(domain='swe_bench', model_name='gpt-4')
```

**工作流程**:
1. `GrepSearch`: 搜索相关代码
2. `FileRead`: 读取文件
3. `DirectAnswer`: 生成 patch
4. `GitDiff`: 提取 diff

---

## 容器内交互操作

### 文件操作 (基于 EvoMAS 经验)

| 操作 | 说明 | 用法 |
|------|------|------|
| `FileRead` | 读取文件内容 | `{"files_to_read": ["src/main.py"]}` |
| `FileWrite` | 写入文件 | `{"writes": [{"path": "src/main.py", "content": "..."}]}` |
| `BashCommand` | 执行 bash 命令 | `{"commands": ["ls -la", "python test.py"]}` |
| `GrepSearch` | 搜索代码模式 | `{"patterns": ["def foo", "class Bar"]}` |
| `GitDiff` | 获取 git diff | 直接获取 patch |

### Git 状态管理 (基于 EvoMAS 经验)

```python
# 任务前: checkout 到 base_commit
subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_path)
subprocess.run(["git", "clean", "-fdx"], cwd=repo_path)
subprocess.run(["git", "checkout", base_commit], cwd=repo_path)

# 任务后: 恢复干净状态
subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_path)
subprocess.run(["git", "clean", "-fdx"], cwd=repo_path)
```

### RepoLock (基于 EvoMAS 经验)

防止并发访问同一仓库：

```python
from swarm.environment.domain.swe_bench.env import RepoLock

lock = RepoLock("/path/to/repo")
with lock:
    # 执行任务
    pass
```

---

## 运行流程

### 1. 环境准备

```bash
# 安装依赖
pip install -e . --break-system-packages

# 或使用 poetry
poetry install
```

### 2. 基线测试 (DirectAnswer)

```bash
PYTHONPATH=. python experiments/run_swebench.py \
    --mode DirectAnswer \
    --data-path ./datasets/swebench/verified_new_split.json \
    --split test \
    --limit 10
```

### 3. FullConnectedSwarm 基线 (推荐)

```bash
PYTHONPATH=. python experiments/run_swebench.py \
    --mode FullConnectedSwarm \
    --agent-type SWECodeAgent \
    --data-path ./datasets/swebench/verified_new_split.json \
    --split test \
    --num-truthful-agents 1 \
    --limit 10
```

### 4. 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--mode` | DirectAnswer | 运行模式 |
| `--agent-type` | SWECodeAgent | Agent 类型 |
| `--data-path` | None | 本地数据路径 |
| `--split` | test | 数据划分 |
| `--limit` | None | 限制实例数量 |
| `--num-truthful-agents` | 1 | Truthful agent 数量 |
| `--repo-cache-dir` | ./repos | 仓库缓存目录 |
| `--output-dir` | ./outputs/swebench | 输出目录 |
| `--debug` | False | 调试模式 |

### 5. Agent 类型选择

| Agent | 说明 | 适用场景 |
|-------|------|----------|
| `SWECodeIOAgent` | 直接生成 patch，不看代码 | 基线 |
| `SWECodeAgent` | 读取代码后生成 patch | **推荐** |
| `SWEReActAgent` | ReAct 风格多步推理 | 复杂任务 |
| `SWEMultiStepAgent` | 搜索 + 读取 + 分析 | 多文件分析 |

---

## 断网环境配置

### 数据准备

1. 从 HuggingFace 下载完整数据：
```bash
python3 -c "
from datasets import load_dataset
import json

dataset = load_dataset('princeton-nlp/SWE-bench_Verified', split='test')
# 过滤需要的实例...
# 保存到 verified_new_full.json
"
```

2. 确保仓库镜像已准备：
   - django
   - matplotlib
   - sphinx-doc
   - sympy

### 运行断网实验

```bash
docker run \
    --network none \
    --rm \
    -v /path/to/dataset:/app/dataset \
    -v /path/to/repos:/app/repos \
    -v /path/to/output:/app/output \
    my-eval-image:latest \
    python experiments/run_swebench.py \
    --mode FullConnectedSwarm \
    --agent-type SWECodeAgent \
    --data-path /app/dataset/verified_new_split.json \
    --repo-cache-dir /app/repos \
    --output-dir /app/output
```

---

## 评估结果格式

### 预测结果

```json
{
  "instance_id": "django__django-10999",
  "model_patch": "diff --git a/... ..."
}
```

### 评估报告

```json
{
  "model_name": "gpt-4",
  "total": 154,
  "resolved": 10,
  "unresolved": 144,
  "empty_patch": 5,
  "errors": 2,
  "resolved_ids": ["django__django-10999", ...],
  "resolution_rate": 0.065
}
```

---

## 关键提示 (来自 EvoMAS 经验)

### 1. Repo 锁定

同一仓库的任务应分配给同一 Worker，避免并发问题：
- 使用 `RepoLock` 进行文件锁
- 仓库级并行而非实例级并行

### 2. Git 状态管理

每个任务前后需清理：
```bash
# 任务前
git reset --hard HEAD
git clean -fdx
git checkout <base_commit>

# 任务后
git reset --hard HEAD
git clean -fdx
```

### 3. Patch 处理

LLM 输出可能包含：
- Markdown 代码块标记
- 额外的解释文本
- 截断的 diff

使用 `PatchParser.clean_patch()` 清理。

---

## 输出目录结构

```
outputs/swebench/
├── predictions_20240916_120000.json
├── predictions_cleaned.json
├── reports/
│   └── evaluation_*.json
└── evaluation_summary.json
```

---

## 已知限制

1. **依赖问题**: 需要安装完整的 GPTSwarm 依赖
2. **仓库访问**: 首次运行可能需要访问网络克隆仓库（之后可断网）
3. **评估方式**: 精确评估需要安装 `swebench` 包或使用 Docker

---

## 下一步计划

- [ ] 完整安装依赖后进行端到端测试
- [ ] 对比不同 Agent 的性能
- [ ] 集成 Docker 评估模式
- [ ] 添加更多数据集变体支持

---

*创建时间: 2024-09-16*
*项目: GPTSwarm*
