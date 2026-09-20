# SWE-bench 镜像检查结果

## 检查日期: 2024-09-16

### 镜像状态

| 仓库 | 本地镜像 | 缺失 | 状态 |
|------|---------|------|------|
| django | 100 | 0 | ✅ 完整 |
| matplotlib | 34 | 0 | ✅ 完整 |
| sphinx | 0 | 44 | ❌ Docker Hub 不存在 |
| sympy | 70 | 0 | ✅ 完整 |

### 总计
- 本地存在: 204
- 缺失: 44 (全部为 sphinx)

### 结论
`sphinx-doc` 仓库的镜像在 Docker Hub 上不可用。这是 SWE-bench Verified 新版本中的特殊情况。

### 解决方案

#### 方案 1: 跳过 sphinx 测试
使用 `verified_146_split.json` 数据集（不含 sphinx）：
```bash
python3 scripts/check_swebench_images.py datasets/swebench/verified_146_split.json
```

#### 方案 2: 从源码构建
如果确实需要测试 sphinx，可以手动构建镜像或使用其他评估方式。

### 推荐测试配置

对于断网实验，推荐使用 `verified_146_split.json`：
- django: 30 个 opt + 29 个 test + 1 个 smoke
- matplotlib: 7 个 opt + 24 个 test + 1 个 smoke
- scikit-learn: 7 个 opt + 24 个 test + 1 个 smoke
- sympy: 8 个 opt + 30 个 test + 1 个 smoke

总计: 146 个实例，全部镜像就绪。
