# Simpler Perf Tracker

> 面向 [hw-native-sys/simpler](https://github.com/hw-native-sys/simpler) 的 PR 级 NPU 性能追踪工具。

工具会逐个检出 `main` 上的 squash commit，在一张或多张 NPU 上重新构建并执行
benchmark，并额外测量 `host_build_graph` 的 Qwen3-14B 与 DeepSeek-V4 FLASH
bind control plane，计算相邻 PR 的性能变化，最终生成 Markdown/JSONL 报告，也可以
增量发布到飞书。

> 适合用来回答：最近哪个 PR 让 Device、Orchestration 或 HBG host bind 耗时发生了变化？

当前主要支持：

- 按最近 PR 数量或时间窗口选择 commit，并用独立 worktree 隔离构建；
- 在多张 NPU 上并行测试，遇到异常设备时自动换卡重试；
- 每个 commit 以 6 轮分别测量 Qwen3-14B（一卡）和 DeepSeek-V4 FLASH（两卡）的
  HBG host bind phases；
- 汇总性能变化、生成本地报告，并按 commit SHA 去重发布飞书月报。

安装后的统一入口是 `pto-simpler-perf-tracker`；不安装时也可直接运行仓库中的
`./run.sh`，运行数据会写入 Git 忽略的 `runtime/`。

## 工作流程

```text
simpler/main commits
        │
        ▼
按 PR 创建独立 worktree ──► 重新构建 ──┬─► Device benchmark
                                        ├─► Qwen HBG host bind
                                        └─► DeepSeek HBG host bind
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

## 核心机制

整个工具可以简单理解为“选择 commit → 隔离测试 → 汇总对比 → 增量发布”：

1. **选择 commit**
   工具从 `upstream/main` 读取指定时间窗口或最近 N 个 commit。由于项目采用
   squash merge，每个 commit 可以直接视为一个 PR，无需再调用 GitHub API 查询 PR。

2. **隔离构建和测试**
   每个 commit 都使用独立的 Git worktree，避免不同版本的源码、构建目录和 Python
   环境互相污染。工具先在普通用户进程中重新构建 simpler；构建完成后才通过
   `task-submit` 执行项目自带的 `benchmark_rounds.sh`，随后按
   `.claude/skills/hbg-bind-phases` 的 numbers 口径分别执行 Qwen 和 DeepSeek case；
   编译期间不占用 NPU。构建和已分配设备的测试都会由工具显式加载 CANN 环境，
   不依赖 cron 或交互 shell 的 `LD_LIBRARY_PATH`。

3. **按 NPU 并行分片**
   多卡运行时，commit 列表会按连续区间拆成多个 shard；每个 benchmark 就绪后通过
   `task-submit --device auto` 自动申请空闲 NPU，结束立即释放。失败 commit 会重试，
   不要求整批任务重跑；跨卡边界不计算性能 Δ，避免把卡间差异误报成回归。

4. **原始数据与报告分离**
   benchmark 结果先按完成顺序追加到原始 JSONL/Markdown，确保中断时已完成的数据仍然
   可用。后处理再负责合并重试记录、恢复 commit 顺序、标记设备，并计算相邻 commit
   的 Device/Orchestration 耗时变化。

5. **按 SHA 增量发布**
   飞书发布状态保存在 `work/` 中。已发布的 commit SHA 不会重复插入；日常任务因此可以
   使用重叠的 36 小时时间窗口，在自动补漏的同时避免生成重复报告。

为保证趋势一致，已经发布过的 commit 会沿用首次发布的测量值。这样即使后续重叠窗口
再次测到同一 commit，更新 commit 的性能 Δ 仍然与飞书中已经展示的基线一致。

## 安装与目录规范

工具名与远程仓库同为 `pto-simpler-perf-tracker`，唯一公开命令是
`/usr/local/bin/pto-simpler-perf-tracker`。源码始终保留在 Git 仓库；正式安装默认布局为：

```text
/home/pypto-tools/pto-simpler-perf-tracker/
├── app/       # 由安装器更新的程序文件
├── config/    # perf-tracker.env；重装不覆盖
├── state/     # clone、worktree、报告和发布状态
├── logs/      # cron 等运行日志
└── tmp/       # 临时文件
```

首次安装并生成配置模板：

```bash
sudo ./install.sh --init-config
sudoedit /home/pypto-tools/pto-simpler-perf-tracker/config/perf-tracker.env
pto-simpler-perf-tracker --recent 1 -m 1 -r 10
```

后续升级只需再次执行 `sudo ./install.sh`。安装器只替换 `app/`，不会覆盖 `config/`，
也不会删除 `state/`；安装过程不会启动 benchmark、联网、发布消息或执行其他业务操作。
可用 `--tools-root DIR` 改变部署根目录。`--bin-dir DIR` 主要供打包和隔离测试使用。

直接从源码运行时不需要安装，`./run.sh` 会使用仓库内被 Git 忽略的 `runtime/`：

```text
runtime/{config,state,logs,tmp}
```

源码模式配置文件是 `runtime/config/perf-tracker.env`；安装模式配置文件是
`/home/pypto-tools/pto-simpler-perf-tracker/config/perf-tracker.env`。也可用
`PTO_CONFIG_FILE` 显式指定配置文件。配置中不要写入日志或提交到 Git。

## 快速开始

### 1. 准备环境

运行机器需要：

- 可用的 NPU 和在 `PATH` 中的 `task-submit`；
- Python 3；
- simpler 的构建依赖，例如 `scikit-build-core`、`nanobind`、CMake 和 Ninja；
- 能访问 GitHub。工具会依次尝试 SSH、本机 HTTP 代理 `4780/4781` 和直连 HTTPS。

先激活包含 simpler 构建依赖的 Python 环境，然后执行：

```bash
git clone https://github.com/pypto-tools/pto-simpler-perf-tracker.git
cd pto-simpler-perf-tracker

# 最小验证：最近 1 个 PR，Device case 10 轮，两个 host case 各 6 轮
./run.sh --recent 1 -m 1 -r 10
```

源码模式会自动把 simpler clone 到 `runtime/state/work/simpler`，不要求从 simpler
仓库内运行；安装模式则写到统一部署目录的 `state/work/simpler`。

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
| `--host-rounds N` | 每个 HBG host case 的轮数；每个 rank 的首 bind 会丢弃 | `6` |
| `--host-case CASE` | 只测指定 host case，可重复；`qwen3-14b` / `dsv4-flash` | 两者 |
| `--host-only` | 快速验证时跳过原 Device benchmark，只采 host | 关闭 |
| `--no-host` | 关闭额外的 HBG host 测量 | 关闭 |
| `--workdir DIR` | clone、worktree 和报告目录 | `<state>/work` |
| `--push` | 将处理后的结果增量发布到飞书 | 关闭 |

默认窗口使用 36 小时而不是 24 小时，让下一次日常任务可以补回一次失败的运行。
重复窗口不会在飞书中产生重复记录，发布逻辑会按 commit SHA 去重。

## 输出

所有持久运行数据默认写入 `<state>/work/`。源码模式的整个 `runtime/` 都不会提交到 Git：

| 文件或目录 | 内容 |
| --- | --- |
| `perf_history.md` / `.jsonl` | 原始结果，逐 commit 追加写入 |
| `perf_history_processed.md` / `.jsonl` | 去重、排序并计算 Δ 后的最终报告 |
| `perf_shard_*.log` | 各 NPU shard 的执行日志 |
| `perf_logs/<sha>/.../host_bind_*_raw.txt` | 带 `[stamp]` 的 HBG bind phase 原始日志 |
| `simpler/` | 工具管理的 simpler clone 和 benchmark worktree |

原始文件只追加、不改写；后处理始终输出到 `*_processed.*`。并行写入使用 `flock`
串行化，任务中断后可以保留已完成 commit 的结果。

Host 报告对每个 case 展示 `control_plane`、`host_orch`、`graph_upload` 和
`arena_h2d` 的 min/median/max。统计会按 rank 丢掉首个 cold bind；control-plane
先在每个 warm bind 内求和，再跨 bind 取最小值，不会把不同 bind 的 phase minima
相加。相邻 commit 的 host Δ 只在 allocation 设备一致时展示；共享主机噪声较大，
该趋势用于筛查，精确 A/B 仍应按 Simpler 技能要求交错测量。

## 飞书发布（可选）

源码模式可复制配置模板并填入自己的应用凭据：

```bash
mkdir -p runtime/config
cp .env.example runtime/config/perf-tracker.env
```

至少配置：

```dotenv
FEISHU_APP_ID=cli_xxxxxxxx
FEISHU_APP_SECRET=xxxxxxxx
FEISHU_WIKI_TOKEN=xxxxxxxx
```

也可以使用 `FEISHU_DOCX_TOKEN` 指向普通文档。正式安装推荐使用
`./install.sh --init-config` 创建权限为 `0600` 的配置。不要把真实密钥提交到仓库，
也不要将配置内容输出到日志。

使用 `--push` 后，工具会维护按月拆分的性能报告和索引，并分别发布：

- 带相邻 commit 性能 Δ 的追踪报告；
- 只包含原始实测值的报告。

如需在失败时发送飞书私信，可额外配置 `.env.example` 中的
`NOTIFY_RECEIVE_ID` 和 `NOTIFY_RECEIVE_TYPE`。

## GitHub CI 每周性能

`ci_weekly_report.py` 是独立于 NPU benchmark 的周报模块。它通过 GitHub REST API
读取 `hw-native-sys/simpler` 的 PR CI，分别统计四个目标 job 在 OS、执行路径和匿名
runner 性能档位上的 p50/p90，并维护以下飞书文档层级：

```text
Simpler 总索引
└── GitHub CI 性能索引
    ├── 2026-W35
    ├── 2026-W34
    └── ...
```

每周文档保存 job wall time、阶段时间、成功样本数和最慢 run 链接。GitHub issue
`#1772` 只维护一条带隐藏标记的评论，每次覆盖本周摘要；完整历史保留在飞书。
工具只重建自己创建的 CI 索引和周文档，对人工维护的 Simpler 总索引只幂等追加一条
CI 索引链接。

配置文件中需要：

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_SIMPLER_INDEX_WIKI_TOKEN=xxx
CI_RUNNER_TIERS_JSON='{"1001":"standard","1002":"slow"}'
CI_RUNNER_ANON_SALT=本机随机值
```

GitHub 认证默认复用服务器已有的 `gh auth` 登录，不需要在 tracker 配置中再保存一份
token；`GITHUB_TOKEN`/`GH_TOKEN` 只作为可选覆盖。现有登录需要具备目标仓库的
Actions 读取和 issue 写入权限。飞书继续复用 tracker 已有的 `FEISHU_APP_ID` 和
`FEISHU_APP_SECRET`。`FEISHU_SIMPLER_INDEX_*` 只标识人工维护的 Simpler 总索引，
刻意不复用其他报告的 `FEISHU_WIKI_TOKEN`/`FEISHU_DOCX_TOKEN`，避免把 CI 入口写进
错误文档；它不是新的认证凭据。runner 映射的 key 可以是 GitHub API 返回的 runner
id 或 name，value 是对外展示的匿名档位；
未配置的 runner 会分别显示为稳定的 `unclassified-xxxxxxxx`，不会泄露或合并真实名称。

先生成本地报告验证口径，不访问飞书、也不修改 issue：

```bash
./ci_weekly.sh --week 2026-W35
```

确认后发布。统计周期固定为北京时间周一 00:00（含）到下一周周一 00:00（不含），
即完整覆盖周一至周日；省略 `--week` 时使用网络时间选择上一个完整自然周：

```bash
./ci_weekly.sh --publish
```

如需在周日深夜生成本周快照，使用 `--current-week`。该选项仅供显式的周日调度使用，
不会改变手工运行时默认选择上一完整周的行为：

```bash
./ci_weekly.sh --current-week --publish
```

本地脱敏快照保存在 `<state>/ci-weekly/YYYY-Www.{json,md}`，发布状态保存在
`<state>/ci-weekly-state.json`。重复执行同一周不会创建重复飞书文档或 issue 评论；确需
按新数据重建某周文档时使用 `--week YYYY-Www --publish --force`。

选周和报告生成时间始终来自 HTTP `Date` 网络时间，包括显式传入 `--week` 的情况；
网络时间不可用时直接退出，不回退到服务器时钟，也不会写飞书或 issue。

本机时钟不可信时，可让现有网络时间调度器在北京时间每周日 23:55 运行一次。
`--current-week` 让本次任务统计即将结束的本周，周报模块自身仍按 week 幂等：

```cron
CRON_TZ=Asia/Shanghai
55 23 * * 0 python3 /home/pypto-tools/pto-simpler-perf-tracker/app/scheduled_run.py --window 23:50-00:10 --lock /home/pypto-tools/pto-simpler-perf-tracker/state/ci-weekly.lock --stamp /home/pypto-tools/pto-simpler-perf-tracker/state/ci-weekly-weekly.stamp -- /home/pypto-tools/pto-simpler-perf-tracker/app/ci_weekly.sh --current-week --publish >> /home/pypto-tools/pto-simpler-perf-tracker/logs/ci-weekly.log 2>&1
```

## GitHub CI 每日偶现失败扫描

`ci_daily.sh` 只读扫描前一个北京时间自然日的目标 CI job，并在后续运行中复查尚未确认的失败。它不会启动、重跑、取消或修改任何 GitHub Actions。

```bash
# 使用网络时间扫描前一个完整自然日
./ci_daily.sh

# 先用固定日期查看报告格式
./ci_daily.sh --day 2026-09-20
```

结果保存在 `runtime/state/ci-daily/`（安装模式为部署目录下的 `state/ci-daily/`）。飞书发布按 ISO 周分文档：
总索引只保留周入口，每周一个日报文档，避免单个文档无限增长。

```text
pending.json       # 等待后续重跑结果的失败
2026-09-20.json     # 机器可读日报
2026-09-20.md       # 人工查看日报
feishu-state.json   # 飞书索引、月度文档和已发布日期
```

日报分为“重跑后恢复”、“待复查失败”和“多个 PR 的共性问题”三部分。共性问题只聚合最近 3 天的数据，按归一化后的 `pattern_id` 汇总，至少出现在两个不同 PR、分支或 commit 才展示，并列出变更数、CI run 数、出现次数和重跑恢复次数。启用通知后，日报会追加到独立的“简化 GitHub CI 每日扫描”Feishu 文档，私信附带该日报文档链接，不混入每周 CI 文档。日志只提取脱敏后的失败签名和少量证据行，不保存完整日志。

## NPU 故障恢复

共享 NPU 可能在测试过程中因 kernel 超时进入异常状态。每个 commit 构建完成后才会
通过 `task-submit` 申请设备，benchmark 结束即释放；失败 commit 在下一轮换卡重试：

- 优先选择 `task-submit --list` 中空闲的卡；
- 同一 commit 在多张卡上失败后，才判定为 commit 自身失败；
- 重试产生的多条记录由后处理合并，并优先保留成功结果。
- 固定范围回填保留此前严格成功的数据，中断后不会从零开始；
- 客户端等待时间长于 benchmark 硬超时，后台任务未结束时不会删除 worktree。

可通过环境变量调整恢复策略：

| 环境变量 | 说明 | 默认值 |
| --- | --- | --- |
| `PERF_MAX_ROUNDS` | 最多重新调度轮数 | `4` |
| `PERF_MAX_ATTEMPT` | 单个 commit 最多尝试的不同设备数 | `3` |
| `PERF_TASK_TIMEOUT` | 兼容旧配置：单个 benchmark 的硬超时 | `3600s` |
| `PERF_TASK_MAXTIME` | 单个 benchmark 的硬超时（优先于上项） | `3600s` |
| `PERF_TASK_WAIT_TIMEOUT` | `task-submit` 客户端等待上限 | `24h` |

### CANN 运行环境

工具启动时会定位 CANN 的 `set_env.sh`，并在每个已经取得设备 allocation 的
`task-submit` benchmark 内再次显式加载。随后在同一个 allocation 中运行 simpler
自带的 `onboard-arch-precheck`，确认实际芯片与 `--platform` 匹配。默认依次检查：

1. `ASCEND_HOME_PATH`、`ASCEND_TOOLKIT_HOME`、`CANN_HOME` 指向目录中的
   `set_env.sh`；
2. `/usr/local/Ascend/cann/set_env.sh`；
3. `/usr/local/Ascend/ascend-toolkit/{set_env.sh,latest/set_env.sh}`。

安装路径非标准时，在 `perf-tracker.env` 中配置绝对路径：

```dotenv
PERF_CANN_ENV_SCRIPT=/path/to/cann/set_env.sh
```

显式路径不存在时任务会在创建 worktree 前直接失败。benchmark 启动后还会在已分配
设备的 job 内先导入一次 `torch_npu`；这样动态库缺失会作为单一的 CANN preflight
错误立即暴露，不再逐个 case 重复失败。

## 定时运行

服务器本机时钟不可信时，不要只靠 `CRON_TZ` 判断执行窗口。安装包中的
`scheduled_run.py` 会读取 HTTP `Date` 响应头，显式换算为北京时间，并且只在
12:30–13:30 或 22:00–次日 08:00 启动任务。网络时间不可用时会直接跳过，绝不
回退到服务器时间执行 benchmark。

cron 可以每 30 分钟轻量唤醒一次调度器；窗口外只做网络时间校验，不占用 NPU，
同一北京时间逻辑日成功后不会重复运行：

```cron
*/30 * * * * source /path/to/conda.sh && conda activate YOUR_ENV && python /home/pypto-tools/pto-simpler-perf-tracker/app/scheduled_run.py --lock /home/pypto-tools/pto-simpler-perf-tracker/state/run.lock --stamp /home/pypto-tools/pto-simpler-perf-tracker/state/daily.stamp -- /usr/local/bin/pto-simpler-perf-tracker --push >> /home/pypto-tools/pto-simpler-perf-tracker/logs/cron.log 2>&1
```

调度锁会阻止日常任务、回填任务重叠。报告时间戳仍会优先读取 HTTP `Date` 响应头；
网络不可用时报告生成可回退本机时间并输出 `LOCAL-FALLBACK`，但调度器本身不会回退。

## 主要文件

| 文件 | 用途 |
| --- | --- |
| `run.sh` | 总入口：更新仓库、运行 benchmark、处理结果、可选发布 |
| `ci_weekly.sh` | 加载私有配置并运行独立的 GitHub CI 周报模块 |
| `ci_daily.sh` | 运行只读的每日 CI 偶现失败扫描 |
| `perf_history_parallel.sh` | 将 commit 连续分片到多张 NPU，并负责续跑和换卡重试 |
| `perf_history.py` | 为每个 commit 创建 worktree、构建，并采集 Device 与两个 HBG host case |
| `perf_finalize.py` | 结果去重、设备标记、排序和相邻 commit Δ 计算 |
| `feishu_perf_report.py` | 将处理后的结果发布到飞书文档或 Wiki |
| `ci_weekly_report.py` | 采集 GitHub CI 时间并维护飞书周报、索引与 issue 看板 |
| `ci_daily_report.py` | 采集前一天失败并确认后续重跑是否恢复 |
| `notify_feishu.py` | 发送运行失败通知 |
| `nettime.py` | 获取不依赖本机系统时钟的报告时间 |
| `backfill.sh` | 历史数据回填辅助脚本 |
| `install.sh` | 安装或升级 `app/`，并维护唯一公开命令 |
| `runtime_paths.sh` | 解析源码/安装模式的配置与状态目录 |

如果机器不需要 `task-submit` 就能直接使用 NPU，也可以单独调用核心脚本：

```bash
python perf_history.py --repo /path/to/simpler -d 0 [其他参数]
```

## 验证

安装与目录行为测试不运行 benchmark，也不访问外部服务：

```bash
bash tests/test_install.sh
python3 -m unittest discover -s tests
git diff --check
```
