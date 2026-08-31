# PostgreSQL 持久层替换计划

> 状态：已实施并验证 · 计划日期：2026-08-27 · 验证日期：2026-08-28 · 基线：`main@origin` (`96ee4e9a`)
>
> 本次只替换数据库，不实施控制面/worker 分离或重做聊天模型。实施前需在 draft PR 上获得明确批准。

## 1. 背景与目标

当前 `Store` 使用 stdlib `sqlite3` 和单个本地数据库文件。这个实现适合 M0 单机验证，但数据库文件
不能作为未来独立控制面与 worker 之间的网络共享边界。先把持久层换成 PostgreSQL，可以去掉这一项
部署约束，同时不提前引入远程调度等更大的架构改动。

本次目标：

- PostgreSQL 成为唯一运行时数据库，继续保存现有 flow、attempt、job、event、Codex run 和 review 数据；
- 保持 `Store` 对上层的行为和领域模型基本不变，避免数据库替换扩散成业务重构；
- 使用 PostgreSQL 事务完成 job claim，使两个数据库连接不会取到同一个 queued job；
- 让两个独立 `Store` 实例能看到同一份状态，为以后拆进程提供数据库基础；
- 提供可复制的本地 PostgreSQL 启动、配置、测试和 CI 路径。

明确不在本次范围内：

- 拆分 `flowtool serve` 与 worker 进程，或引入远程 worker、消息队列、`JobDispatch` adapter；
- worker affinity、分布式 lease/fencing、多 worker 调度和 Codex session 跨机器接管；
- 新建聊天消息表、修改 prompt/session 语义，或持久化 Codex 原始 JSONL；
- 把整个 Store 调用链改成 async、引入 ORM/Alembic，或增加新的业务 API；
- 自动迁移现有 SQLite 文件。项目仍处于 M0，旧文件保留给人工归档；如确有需保留的数据，另行设计一次性迁移工具。

## 2. 技术方案

### 2.1 PostgreSQL adapter

- 用 Psycopg 3 和 `psycopg_pool.ConnectionPool` 替换 `sqlite3.Connection`；每个 Store 操作从池中取得连接，
  并在明确的事务边界内提交或回滚。
- 保留同步 `Store` 接口和当前调用方。远程数据库 I/O 的全面 async 化属于独立性能改造，不与本次
  后端替换捆绑。
- 将 `FLOWTOOL_DB_PATH` 替换为 `FLOWTOOL_DATABASE_URL`。URL 可包含 `sslmode` 等 PostgreSQL 参数，
  但不得写入日志；远程部署文档要求 TLS 和最小权限数据库账号。
- `Store.init_db()` 继续负责 M0 初始 schema 的幂等创建，暂不建设通用迁移框架。schema 后续第一次需要
  演进时再单独引入版本化迁移，避免现在同时维护两套未被使用的机制。

### 2.2 Schema 对等迁移

把现有六张表和约束翻译为 PostgreSQL 原生类型，保持上层语义：

- 自增主键使用 identity，时间使用带时区 timestamp，结构化 payload 使用 JSONB；
- 保留 active flow 的 partial unique index、attempt/flow 外键、review item 唯一约束和现有查询顺序；
- `INSERT ... RETURNING` 直接取得新记录 ID，移除 SQLite 专用的 `lastrowid`、PRAGMA、row factory 和
  ISO 文本时间转换；
- `claim_job()` 在一个事务中使用带 `FOR UPDATE SKIP LOCKED` 的确定顺序查询并更新选中的 job，
  防止并发连接重复 claim；heartbeat、finish 和 stale recovery 维持现有单 worker 语义，不扩展为 lease 协议。

表名、字段含义和 `Store` 方法不会借机重新设计。`codex_runs.prompt`、`review_threads.body` 与 event payload
仍按当前行为保存；本次只是更换承载它们的数据库。

### 2.3 开发与运行环境

- 增加最小的 Compose 配置，提供本地开发/测试 PostgreSQL；数据库数据使用命名 volume，示例凭据只用于本机。
- `env.example` 和快速开始改用 PostgreSQL URL，并在启动应用前执行 Compose 启动步骤。
- pytest 使用专用测试数据库，fixture 在用例间清空数据；持久层测试使用真实 PostgreSQL，不用 SQLite
  或 mock SQL 来证明 PostgreSQL 行为。
- GitHub Actions 的 pytest check 增加 PostgreSQL service 和测试 URL；Ruff、Mypy、build 不依赖数据库服务。

## 3. 变更边界

批准后保留本计划 change 为栈底，并在一个子 change 中交付一个内聚结果：

1. **`feat(store): replace SQLite with PostgreSQL`**
   - 更新依赖、配置、Store schema/查询和应用装配；
   - 更新持久层、配置及受影响的入口/worker 测试，增加跨 Store 可见性和并发 claim 场景；
   - 增加本地 PostgreSQL Compose 配置并让 pytest CI 使用 PostgreSQL service；
   - 同步 README、环境变量示例、架构正文/可视化、计划状态和测试基线。

数据库配置、实现、验证环境和当前事实文档共同构成一次可用替换，没有独立交付或回退价值，因此不人为
拆成多个实现 change。若实施发现必须新增通用迁移系统或分布式 worker 协议，将先回到本计划重新审批。

## 4. 验证策略

实施阶段至少验证：

1. 空 PostgreSQL 数据库可幂等初始化完整 schema；
2. 现有 active flow 唯一约束、flow/attempt/job 往返、review upsert/relay/resolve 和 stale recovery 测试通过；
3. 两个独立 Store/连接池能读写同一 flow 与 review/Codex run 数据；
4. 两个并发 claim 不会获得同一个 queued job；
5. 错误数据库 URL 或不可连接数据库会在启动时明确失败，而不是回退到本地 SQLite；
6. `uv run pytest`、`uv run ruff check app tests`、`uv run mypy app tests` 和
   `uv build --no-sources` 全部通过；
7. 使用 Compose PostgreSQL 启动真实 `flowtool serve`，`GET /healthz` 返回 200，并能正常关闭；
8. 草稿 PR 上 Ruff、Mypy、pytest 与 Build 四项 CI checks 全部通过。

测试不访问 GitHub、Codex 或 bub，也不使用真实服务凭据。并发测试只验证 PostgreSQL claim 的互斥，
不把尚未实现的多 worker lease/故障接管算作验收范围。

## 5. 文档影响

实施时预计同步：

- `README.md`：当前能力、快速开始、验证基线和 SQLite 表述；
- `env.example`：移除 `FLOWTOOL_DB_PATH`，增加 PostgreSQL URL 示例；
- `docs/architecture/overview.md`、`components.md`、`workflows.md`、`decisions.md`、`roadmap.md`：
  把当前持久层改为 PostgreSQL，同时继续明确控制面/worker 尚未拆分；
- `docs/architecture/README.md` 与 `diagram.html`：同步组件状态、节点文字和测试统计；
- `docs/plan/README.md` 与本计划：实施后移动到已完成并记录实际结果；
- `.github/workflows/ci.yml`：仅为 pytest check 提供 PostgreSQL service。

仓库没有 changelog 或文档生成器，本次不为数据库替换新建这些体系。历史 M0 计划继续保留当时使用
SQLite 的事实，不改写历史，只在当前事实文档和本计划中记录替换结果。

## 6. 风险与回退

- **破坏性配置切换**：`FLOWTOOL_DB_PATH` 不再生效；启动时缺少/错误 URL 必须明确报错，文档给出迁移边界。
- **同步网络 I/O**：本次沿用同步 Store 以控制范围；若实际延迟影响事件循环，再以数据为依据单独 async 化。
- **连接与事务泄漏**：所有方法通过 pool context 管理连接，并由失败路径测试验证回滚后连接可继续使用。
- **测试环境成本**：真实 PostgreSQL 增加一项本地依赖；Compose 和 CI service 提供一致、可重复的入口。
- **误判分布式能力**：PostgreSQL 只移除共享文件限制，不自动解决 worker 会话归属、lease fencing 或外部副作用幂等；
  文档继续把这些列为后续能力。

若实现无法稳定替换，回退本子 change 即恢复原 SQLite 代码和配置；计划 change 仍保留为决策记录。

## 7. 实施结果

- PostgreSQL 已成为唯一运行时持久层；配置改为 `FLOWTOOL_DATABASE_URL`，Store 使用同步 Psycopg 3
  连接池，并用 PostgreSQL 原生 schema、JSONB、带时区时间和 identity 主键承载原有六张表。
- job claim 使用单条 `FOR UPDATE SKIP LOCKED` CTE 更新；delivery 去重使用
  `INSERT ... ON CONFLICT DO NOTHING RETURNING` 原子占有。两个独立 Store 的共享可见性、并发 claim
  和 delivery 去重均有集成测试。
- 增加 `compose.yaml`、PostgreSQL 环境变量示例和 CI service。GitHub Actions run
  [33150010124](https://github.com/b1indsight/agentflow/actions/runs/33150010124) 在 PostgreSQL 17.11 上
  通过 23 项 pytest（3.01 秒、无警告），随后启动真实 `flowtool serve`，`/healthz` 返回 200；Ruff、Mypy 和
  Build checks 同时通过。
- 按用户要求，本机没有启动或连接 PostgreSQL。本机 pytest 收集 23 项，其中 6 项非数据库测试通过，
  17 项依赖 `FLOWTOOL_TEST_DATABASE_URL` 的集成测试按预期跳过；本机 Ruff、Mypy、构建、锁文件、CLI
  和文档格式检查通过。真实数据库验收由上述 CI 完成。
- 与计划相比没有扩大架构范围：控制面/worker 仍未拆分，Store 仍为同步接口，daemon 仍在启动时以
  schema owner 身份幂等初始化表，且没有引入迁移框架、worker lease/fencing 或 SQLite 数据导入工具。
  测试隔离实现采用每个 fixture 创建独立 PostgreSQL schema，而不是清空共享表，隔离目标不变。

## 8. 参考依据

- PostgreSQL 文档将 `SKIP LOCKED` 明确列为适合多个消费者访问 queue-like table 时避免锁竞争的机制：
  <https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE>
- Psycopg 官方连接池文档说明 pool 负责限制和复用 PostgreSQL 连接，并通过 connection context 管理事务：
  <https://www.psycopg.org/psycopg3/docs/advanced/pool.html>
