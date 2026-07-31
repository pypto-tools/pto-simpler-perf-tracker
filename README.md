# Simpler Perf Tracker

面向 [hw-native-sys/simpler](https://github.com/hw-native-sys/simpler) 的 PR 级性能追踪工具。
它会逐个检出 `main` 上的 squash commit，在一张或多张 NPU 上重新构建并执行 benchmark，
计算相邻 PR 的性能变化，最终生成 Markdown/JSONL 报告，也可以增量发布到飞书。

> 适合用来回答：最近哪个 PR 让 Device 或 Orchestration 耗时发生了变化？

## 工作流程

```text
simpler/main commits
        │
        ▼
按 PR 创建独立 worktree ──► 重新构建 ──► NPU benchmark
                                            │
                                            ▼
                                  原始 Markdown / JSONL
                                            │
                                            ▼
                              去重、排序、设备标记、计算 Δ
                                            │
                              ┌─────────────┴─────────────┐
                              ▼                           ▼
                         本地报告                    飞书月报（可选）
```

`simpler` 使用 squash merge，因此 `main` 上的一个 commit 就对应一个 PR 的最终状态。
`--recent N` 表示测试最近的 N 个 PR，顺序为从新到旧。

## 快速开始

### 1. 准备环境

运行机器需要：

- 可用的 NPU 和在 `PATH` 中的 `task-submit`；
- Python 3；
- simpler 的构建依赖，例如 `scikit-build-core`、`nanobind`、CMake 和 Ninja；
- 能访问 GitHub。工具会依次尝试 SSH、本机 HTTP 代理 `4780/4781` 和直连 HTTPS。

先激活包含 simpler 构建依赖的 Python 环境，然后执行：

```bash
git clone https://github.com/better-ci/tool-simpler-perf-tracker.git
cd tool-simpler-perf-tracker

# 最小验证：最近 1 个 PR，1 张 NPU，每个 case 运行 10 轮
./run.sh --recent 1 -m 1 -r 10
```

工具会自动把 simpler clone 到 `work/simpler`，不要求从 simpler 仓库内运行。

### 2. 常用命令

```bash
# 日常增量：测试最近 36 小时合入的 PR（默认 1 张 NPU、100 轮）
./run.sh

# 回填最近 100 个 PR，分到 4 张 NPU
./run.sh --recent 100 -m 4

# 指定时间窗口
./run.sh --since '3 days ago'

# 测试后发布到飞书
./run.sh --recent 50 -m 4 --push
```

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `--since DATE` | 测试指定时间之后的 commit | `36 hours ago` |
| `--recent N` | 测试最近 N 个 commit；与 `--since` 二选一 | — |
| `-m M` | 并行 shard / NPU 数量 | `1` |
| `-r ROUNDS` | 每个 benchmark case 的轮数 | `100` |
| `--workdir DIR` | clone、worktree 和报告目录 | `./work` |
| `--push` | 将处理后的结果增量发布到飞书 | 关闭 |

默认窗口使用 36 小时而不是 24 小时，让下一次日常任务可以补回一次失败的运行。
重复窗口不会在飞书中产生重复记录，发布逻辑会按 commit SHA 去重。

## 输出

所有运行数据默认写入 `work/`，该目录不会提交到 Git：

| 文件或目录 | 内容 |
| --- | --- |
| `perf_history.md` / `.jsonl` | 原始结果，逐 commit 追加写入 |
| `perf_history_processed.md` / `.jsonl` | 去重、排序并计算 Δ 后的最终报告 |
| `perf_shard_*.log` | 各 NPU shard 的执行日志 |
| `simpler/` | 工具管理的 simpler clone 和 benchmark worktree |

原始文件只追加、不改写；后处理始终输出到 `*_processed.*`。并行写入使用 `flock`
串行化，任务中断后可以保留已完成 commit 的结果。

## 飞书发布（可选）

复制配置模板并填入自己的应用凭据：

```bash
cp .env.example .env
```

至少配置：

```dotenv
FEISHU_APP_ID=cli_xxxxxxxx
FEISHU_APP_SECRET=xxxxxxxx
FEISHU_WIKI_TOKEN=xxxxxxxx
```

也可以使用 `FEISHU_DOCX_TOKEN` 指向普通文档。`.env` 已加入忽略规则，不要把真实密钥
提交到仓库。

使用 `--push` 后，工具会维护按月拆分的性能报告和索引，并分别发布：

- 带相邻 commit 性能 Δ 的追踪报告；
- 只包含原始实测值的报告。

如需在失败时发送飞书私信，可额外配置 `.env.example` 中的
`NOTIFY_RECEIVE_ID` 和 `NOTIFY_RECEIVE_TYPE`。

## NPU 故障恢复

共享 NPU 可能在测试过程中因 kernel 超时进入异常状态。并行调度器会识别某个 shard
未完成尾部任务的情况，临时弃用该卡，并把尚未完成的 commit 提交到另一张卡：

- 优先选择 `task-submit --list` 中空闲的卡；
- 同一 commit 在多张卡上失败后，才判定为 commit 自身失败；
- 异常卡列表仅在本次运行中有效，不会形成永久黑名单；
- 重试产生的多条记录由后处理合并，并优先保留成功结果。

可通过环境变量调整恢复策略：

| 环境变量 | 说明 | 默认值 |
| --- | --- | --- |
| `PERF_MAX_ROUNDS` | 最多重新调度轮数 | `4` |
| `PERF_MAX_ATTEMPT` | 单个 commit 最多尝试的不同设备数 | `3` |
| `PERF_DEVICES` | 限定设备池，例如 `3,4,5,6` | 全部可用设备 |
| `PERF_TASK_TIMEOUT` | 单个 shard 的排队等待时间 | `6h` |

## 定时运行

下面的 cronie 配置每天北京时间 22:00 执行增量测试并发布到飞书：

```cron
CRON_TZ=Asia/Shanghai
0 22 * * * cd /path/to/tool-simpler-perf-tracker && source /path/to/conda.sh && conda activate YOUR_ENV && PATH=/usr/local/bin:$PATH ./run.sh --push >> work/cron.log 2>&1
```

`CRON_TZ` 只负责时区，机器系统时钟仍应由 NTP 保持准确。报告中的时间戳会优先读取
HTTP `Date` 响应头；网络不可用时才回退到本机时间并输出 `LOCAL-FALLBACK` 提示。

## 主要文件

| 文件 | 用途 |
| --- | --- |
| `run.sh` | 总入口：更新仓库、运行 benchmark、处理结果、可选发布 |
| `perf_history_parallel.sh` | 用 `task-submit` 将 commit 分片到多张 NPU，并负责换卡重试 |
| `perf_history.py` | 为每个 commit 创建 worktree、构建并执行 benchmark |
| `perf_finalize.py` | 结果去重、设备标记、排序和相邻 commit Δ 计算 |
| `feishu_perf_report.py` | 将处理后的结果发布到飞书文档或 Wiki |
| `notify_feishu.py` | 发送运行失败通知 |
| `nettime.py` | 获取不依赖本机系统时钟的报告时间 |
| `backfill.sh` | 历史数据回填辅助脚本 |

如果机器不需要 `task-submit` 就能直接使用 NPU，也可以单独调用核心脚本：

```bash
python perf_history.py --repo /path/to/simpler -d 0 [其他参数]
```
