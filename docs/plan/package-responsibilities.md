# 按独立职责整理 Python 包结构

**Stage:** Implemented — 已完成本地验证与独立审查，待用户评审。
**PR:** [#57](https://github.com/b1indsight/agentflow/pull/57)；bookmark：`refactor/package-responsibilities`
**基线：** `main` / `23329587`（已包含 #56 公平领取锁修复）。

## 1. Initial plan

### 动机与目标

目前 `app/` 顶层混放领域模型、SQL 仓储、GitHub SDK 适配、执行器和运维代码。
`control/` 与 `worker/` 已划分进程职责，但其依赖仍散落顶层；worker 通过
`observability` 的日志入口间接导入 `metrics_store`、`reconcile_store` 和 psycopg。
本次按实际职责归包，使目录能说明模块归属，并以导入边界约束维持进程隔离。

保持业务行为、数据库 schema、持久 JSON、网络协议版本与 CLI 命令不变。
不增加 Web 界面或对话存档；不拆成多个发行包或服务；不借机重写 Store、状态机或 SQL。

### 目标结构与模块迁移

```text
app/
  cli.py                 # 现有命令入口与进程装配
  config.py              # 按角色配置，保留统一入口
  domain/                # 领域事实、纯决策和端口，无运行时 I/O
  contracts/             # 跨进程消息与持久结果应用契约
  persistence/           # PostgreSQL 仓储、schema、只读探针
  github/                # GitHub SDK/GraphQL 适配实现
  control/               # 调度、事务编排、外部副作用、HTTP/WS 服务
  worker/                # 执行、工作目录、VCS、Agent/Judge 子进程
  telemetry/             # 进程通用日志与控制端指标，分模块隔离依赖
  plugins/               # 现有扩展 hooks
  live_smoke.py          # 保留已有运维验收命令入口
```

| 当前模块 | 目标 | 归属理由 |
| --- | --- | --- |
| `models.py` | `domain/models.py` | 保留既有实体与类型，不在本次重新拆分字段 |
| `context_models.py`, `delivery_models.py`, `github_models.py`, `judge_models.py`, `project_models.py`, `review_models.py` | `domain/` 下同名文件 | 共享的事实与业务数据，不依赖具体适配实现 |
| `ports.py`, `review_policy.py`, `recovery_policy.py` | `domain/` 下同名文件 | 端口与可独立调用的纯策略 |
| `protocol.py`, `result_models.py` | `contracts/` 下同名文件 | WebSocket 消息及冻结结果/副作用计划；允许依赖 domain |
| `store.py` 与全部 `*_store.py` | `persistence/` 下同名文件 | SQL、事务、租约与持久化读取，保留现有事务所有权 |
| `readiness.py` | `persistence/readiness.py` | 数据库只读就绪探针 |
| `github_client.py` | `github/client.py` | GitHub App SDK 适配入口 |
| `github_feedback.py`, `github_pagination.py`, `github_thread_pages.py`, `github_threads.py`, `github_projects.py` | `github/` 下去掉 `github_` 前缀 | GitHub 远端读取、分页与解码 |
| `github_retry.py` | `domain/github_retry.py` | 纯退避计算被仓储和控制端共用，不应迫使仓储依赖 SDK 包 |
| `review_context.py` | `control/review_context.py` | 经 GitHub 端口采集、冻结审核证据 |
| `codex_driver.py`, `context_builder.py`, `judge.py`, `judge_runner.py`, `vcs.py`, `credentials.py` | `worker/` 下同名文件 | Agent 执行、prompt、仓库及短时 Git 凭据 |
| `observability.py` | `telemetry/logging.py` 与 `telemetry/metrics.py` | 日志配置无数据库依赖；指标采样只由 control 使用 |

各包 `__init__.py` 保持轻量，不集中重导出所有实现，不保留成套旧路径转发模块。
内部 Python 导入路径随迁移更新；现有 `flowtool` 命令、`app.cli:app`、
`app.control.api`、`python -m app.live_smoke` 保持可用。
Judge 内部子进程入口同步改为 `app.worker.judge_runner`，同时更新测试和启动引用。

### 依赖规则

- `domain` 不依赖 control、worker、contracts、persistence 或 GitHub SDK 实现。
- `contracts` 仅依赖 domain 与基础库；不导入执行器、仓储或适配器。
- `persistence` 依赖 domain/contracts，不能反向依赖 control/worker/github 实现。
- `github` 依赖领域类型、端口与必要配置，不依赖 control/worker/persistence。
- `control` 负责调用仓储、GitHub 适配、协议与插件，不导入 worker 实现。
- `worker` 依赖 domain/contracts、自身执行模块和通用日志，不直接或经日志间接加载
  control、persistence、GitHub SDK 实现；日志模块不能导入 metrics 模块。
- CLI 和各角色 runtime 是装配入口；明确允许的装配依赖不泛化到领域层。

保留现有 INITIAL 校验与 lifecycle/results/effects 之间的防循环约束。
`config.py` 与少量通用类型暂不进一步拆分，避免纯目录整理扩大为模型重构。

### 方案比较与风险

只移动 `*_store`/`github_*` 最省改动，但仍留下执行职责散落与日志引入数据库的问题。
按每个业务功能纵向分包会把共享协议和事务所有权拆散，超出本次范围。
选择沿现有 control/worker 边界归包，且只拆职责确实不同的日志与指标。

风险主要是遗漏字符串模块路径、monkeypatch 目标、子进程入口和部署引用。
迁移同时检索这些引用；更新 README/当前架构说明，历史计划保留原始语境。
不改变序列化字段，持久 worker journal、恢复消息和数据库记录应继续兼容。
Python 内部旧导入路径不作为稳定 SDK；若实施发现实际外部入口，则单独记录兼容决策。

### 实施顺序

1. 先补充有意义的导入隔离回归：worker 日志不引入数据库/控制端实现；验证当前失败原因。
2. 移动 domain/contracts，再移动 persistence/github，更新导入和边界测试路径。
3. 将执行相关模块归入 worker、审核上下文归入 control，拆分日志与指标。
4. 更新动态启动路径、测试 patch 目标、当前架构文档和必要的部署引用。
5. 完成回归、构建、边界消融和审查，在同一 bookmark/PR 中提交实施结果。

### 验证与消融

- 沿用现有行为测试，不为文件改名创建镜像测试；补强职责边界的 AST/导入回归。
- 检查 worker 日志在独立解释器中的传递导入，防止只检查直接 import 而遗漏数据库依赖。
- PostgreSQL 全量 pytest，重点包含恢复、事务、worker journal、Agent 子进程与日志指标。
- `uv run ruff check app tests`、`uv run mypy app tests`、`uv build --no-sources`。
- 验证安装产物中的 CLI、ASGI、Judge 子进程及 smoke 入口，搜索旧导入和模块字符串残留。
- 实施后、代码审查前，临时恢复日志到数据库的依赖及一项跨角色依赖，确认边界检查失败，
  然后撤销实验；纯文件移动不做无意义的性能消融。
- 文档计划阶段只检查 diff 和路径，不宣称运行了实现回归。

### 开放问题与后置工作

无阻塞设计问题。测试目录本次保持现有组织，便于对照迁移前后的同一组行为回归。
Store 内部继续拆分、独立部署依赖裁剪、Web 控制台与对话存档均后置。

## 2. PR change timeline

### 2026-09-30 · 1. 计划批准

用户明确回复“可以”，批准 PR #57 的初始方案。开始实施，保持同一 bookmark 和 PR。

### 2026-09-30 · 2. 职责归包与日志隔离

已按初始映射迁移 35 个模块；新增五个轻量包入口，没有旧路径转发层。
`observability` 拆为通用 logging 和控制端 metrics，worker runtime 只导入 logging。
同步更新直接导入、测试 patch、Judge 子进程入口、Kubernetes schema Job 与恢复 smoke 脚本。
README、组件职责、目录树和架构 HTML 同步使用当前路径；历史计划保持原文。

测试先行：独立解释器加载 worker runtime 的新回归在迁移前失败，证实日志引入 psycopg；
迁移后通过。现有边界检查扩展为六个职责包，同时识别 `from app.worker import vcs` 形式。
原有领域策略约束纳入 domain 包检查，保留 INITIAL 防循环检查，避免重复测试同一约束。

### 2026-09-30 · 3. 验证与边界消融

- PostgreSQL 全量回归：`FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool@127.0.0.1:55419/postgres uv run pytest -q --tb=short`
  最终 **813 passed**。首轮发现线程测试的两项参数情形仍使用旧包导入，修正后重跑全量通过。
- `uv run ruff check app tests`、`uv run mypy app tests`（144 个文件）、`uv build --no-sources`
  与 `git diff --check` 均通过；仅保留既有 Starlette/httpx 弃用告警及 Mypy 提示。
- 35 个迁移模块与基线 `23329587` 做 AST 对比：替换模块路径、忽略 import 顺序后，非导入 AST 一致。
- wheel 安装到本地验证环境，复用项目依赖并确认 `app.__file__` 指向已安装 wheel：CLI help、
  `python -I -m app.live_smoke --help`、ASGI `/healthz` 均通过；从 wheel 执行 Judge 测试 **36 passed**，
  包含真实子进程与本机模拟模型服务。该验证不调用真实模型 provider。
- 消融 A：临时在 `telemetry/logging.py` 添加 `from app.persistence.metrics_store import read_snapshot`，
  执行 `tests/test_boundaries.py::test_worker_startup_does_not_load_control_plane_dependencies`，
  得到 1 failed，检出 persistence/psycopg 传递导入。
- 消融 B：临时在 `control/initial.py` 添加 `from app.worker import vcs`，执行
  `tests/test_boundaries.py::test_packages_keep_responsibility_boundaries`，得到 1 failed / 5 passed，
  精确命中 control 的跨角色依赖。两次实验各自恢复原文件后再运行最终全量回归。

结论：保留日志/指标分离和职责边界检查；没有引入业务分支、协议兼容层或额外抽象。
本轮未做真实 GitHub/Codex 外部闭环或集群部署，包迁移的运行验证范围如上。

### 2026-09-30 · 4. 独立审查

仓库 `review-and-bookmark.sh` 对实现提交 `e57f96895f9e` 完成独立只读审查，
返回空 findings，门禁通过。实现已推送到原 PR #57；本条只补充审查记录，没有修改代码。

### 2026-09-30 · 5. 远端运维 CI 补充检查

远端 recovery Job 已完成 PostgreSQL 17 fixture dump/restore 和 worker home 恢复演练，
随后独立检查 `scripts/smoke/recovery.py` 时检出迁移后的 import 排序问题。
本地原 Ruff 范围是 `app tests`，没有覆盖该脚本；现已修正排序并将脚本纳入检查，
`uv run ruff check app tests scripts/smoke/recovery.py` 与 diff 检查通过。
此次只调整 import 顺序，不改变恢复逻辑；单独通过审查门禁后更新同一 PR。
