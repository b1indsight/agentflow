# 全仓库职责与证据契约收敛 — Feature record

**Stage:** Planning，待用户批准，尚未修改实现或测试。
**Bookmark:** `refactor/repository-ownership-convergence`
**PR:** [#80](https://github.com/b1indsight/agentflow/pull/80)。
**基线:** `main` / `3c7e39a566417270fdf82b49fba5a125eaee81f0`（已合并 #79），2026-10-12。

## 1. Initial plan

### 背景与目标

用户要求沿用 10 月 11–12 日的重构思路优化整个仓库：减少重复模型和入口，
让状态与操作有明确归属，分开单次查询、编排和恢复。本轮检查 control、GitHub、
domain/contracts、worker、持久层与 CLI，并对照实际调用者、相关测试及既有设计记录。

审计发现的修改集中在五处：写入拒绝证据有两种解释；PR 创建回执的唯一性规则重复三次；
启动恢复仍返回无意义的计数；任务合同留有单一子类的中间模型；CLI 导入越过运行边界。
这些问题可以复用现有模块处理，不需要再拆服务或引入通用框架。

目标是删除实际重复的概念和分支，同时让副作用恢复依赖明确的远端证据。
本轮是全仓库审计后选出的具体改动集合；未发现问题的模块不为覆盖目录而修改。

### 拟议行为与验收条件

1. 写入是否明确拒绝由 GitHub 错误模型统一表达。HTTP 200 的 GraphQL 部分失败
   保持写入不确定；错误类别、用于诊断的状态码和未写入证据分别承担各自作用。
2. 三个调用者共用唯一 PR 创建回执规则；缺失或重复回执仍拒绝提交，恢复等待语义保留。
3. 启动恢复只记录离线及恢复期限；实际 sweep 才返回重排、失败计数。
4. `RevisionTask` 直接拥有其全部字段；Assignment 的 Flow 身份校验直接利用已限定的任务联合。
5. 实际 CLI 入口可加载 worker 命令而不提前导入 control、数据库和 GitHub SDK。

验收报告列出删除的概念、生产代码净变化及行为验证。数据库 schema、协议版本、
持久 JSON、任务字段顺序、命令签名和既有重试时间规则继续使用现有契约。

### 技术设计与模块归属

#### A. GitHub 错误保留证据来源，调用层共用拒绝判定

当前 [错误转换与判定](../../app/github/errors.py) 把 GraphQL 业务错误映射为诊断状态码，
`definitely_rejected` 又依据状态码推断本次请求未生效；[Projects](../../app/control/projects.py)
另外使用 `definitely_unsent or request_rejected`。已有
[真实 SDK 部分 mutation 用例](../../tests/test_github_projects.py) 模拟 HTTP 200 同时返回新增 item 和 FORBIDDEN。

- `request_error()` 从 SDK 原始 HTTP 响应设置 `request_rejected`，按现有 Effects 策略
  接受真实 4xx 拒绝，排除 408；该字段表示传输事实，不从业务错误类别反推。
- `GitHubError.definitely_rejected` 统一使用 `definitely_unsent or request_rejected`。
  GraphQL 200 的合成 403/404/429 继续用于分类和诊断，不设置真实 HTTP 拒绝事实。
- Projects 改用同一属性；Effects、Coordinator 保持使用既有属性。
- 保留 [Effects](../../app/control/effects.py) 中历史 `uncertain` 判断与
  `definite_rejection and not uncertain`，后续读取失败不能清除之前的写入不确定性。
  [ResultStore](../../app/persistence/result_store.py) 的持久提交边界不移动。
- 测试替身手造真实 HTTP 错误时显式提供拒绝证据，避免只靠状态码猜测来源。

#### B. 唯一 PR 创建回执规则归现有结果合同

[Effects](../../app/control/effects.py) 的 retired 恢复、正常 PR 依赖通知，以及
[Results](../../app/control/results.py) 的 INITIAL 提交各自筛选回执并检查唯一性。
在已有 [result_models](../../app/contracts/result_models.py) 增加一个小的具名选择函数，
接收已读取的 `EffectRecord` 集合并返回唯一 `PullRequest`，删除三份相同筛选规则。

函数不查询数据库、不绑定资源、不执行 I/O。缺失或重复回执以 `ValueError` 表示；
retired 调用边界转换为 `ResultPending`，保持稍后恢复。正常调用保留契约破坏的错误类别；
INITIAL 仍在事务内读取回执。若传入集合已经过时，不通过额外查询掩盖调用契约。

#### C. 启动恢复删除恒为零的返回和无效日志

[Store.recover_abandoned_assignments](../../app/persistence/store.py) 只标记离线和恢复期限，
始终返回 `(0, 0)`；[ControlRuntime.startup](../../app/control/runtime.py) 据此记录的统计日志永不触发。
方法返回 `None`，runtime 直接调用，删除无效解包和日志分支。
现有启动恢复测试断言持久状态，去除旧零计数断言。
`sweep_expired_assignments()` 的真实统计及日志保持原有归属。

#### D. 任务合同删除无独立消费者的中间层

[protocol](../../app/contracts/protocol.py) 的 `ReviewTask` 只有 `RevisionTask` 一个子类，
没有独立消费者。将 `batch_id`、`attempt_id`、`feedback` 按原字段顺序直接声明在
`RevisionTask(ProtocolModel)`，删除 `ReviewTask`。

`Assignment.task` 已限定为 INITIAL/REVISION/ARCHIVE 联合，三种都拥有 `flow_id`。
身份校验直接比较 `task.flow_id`，删除穷尽全部合法类型的 `isinstance` 分支。
保留字段约束、错误文本、序列化顺序和 JSON Schema；不增加兼容别名或协议升级。

#### E. CLI 导入按实际命令归属

[CLI](../../app/cli.py) 是 `flowtool` 的实际入口，顶层导入 `Store`、`ControlService`、
`archive_progress`，使 worker 命令提前加载 control 和 PostgreSQL 依赖。

- Store 类型放在 `TYPE_CHECKING` 下；实际构造导入归 `_store()`。
- ControlService 导入归 `workers()`、`flow_cancel()`、`flow_retry()`。
- archive_progress 和 DeliveryState 导入归 `flow_show()`。
- 保留同一个 Typer 入口；不新增 facade、装配参数或 CLI 模块。
- 扩充现有 [进程导入边界用例](../../tests/test_boundaries.py)，覆盖 `import app.cli`。

### 方案比较与保留的设计

选择上述改动，是因为它们删除已有重复规则、无效合同或跨职责加载。
只统一 Projects 的一行条件会混淆 GraphQL 部分失败的证据来源，因此先修正错误合同。
PR 回执规则已出现三次且必须同步维护，具名函数比三份实现更简单；不为此新增服务。

审计后保留以下设计：

- WorkerSession 已统一任务状态、恢复和历史入口；拆开历史读取会反转
  [既有归属](worker-session.md) 并增加装配参数。
- ProjectItemsPage.complete 表示不可见内容对缺席证明的影响，不能仅因形状相似就替换为普通 Batch。
- PushCapability/PushSecret、WorkerRecord/WorkerView 保留
  [已有安全与展示边界](internal-contract-simplification.md)。
- 资源的未观察字段保护不改成共用资源基类；符合
  [不可变资源设计](github-capability-boundaries.md)，目前抽象收益不足。
- 连接心跳、身份复查、双层超时和归档重校验分别保护实际边界；不按表面相似合并。
- Store 不按文件长度拆分，SQL、事务和锁顺序继续归持久层；API Protocol 的实际测试替换边界保留。

### 成本、风险与处理

主要风险是把 GraphQL 部分写入误判为未写入，或让后续失败覆盖历史不确定性。
使用原生 SDK MockTransport 与持久恢复用例验证这两种情况。真实 HTTP 4xx 的范围
沿用既有 Effects 策略，Projects 同步采用该明确的请求证据规则。

回执收拢可能把恢复等待变成永久失败；在调用边界保留错误类别与有用的诊断。
任务继承精简可能改变字段顺序或 Schema；实现前后对照实际序列化与 schema。
CLI 延迟导入可能影响测试补丁目标；检查真实命令与既有 `_store` 替换方式。
上述修改不需要数据迁移或额外运行配置。

### 实施顺序

1. 固定当前任务 JSON/Schema 与恢复行为基线，修正拒绝证据来源并迁移 Projects 判定。
2. 收拢唯一 PR 创建回执规则，保留 retired、正常执行和 INITIAL 提交的各自错误策略。
3. 删除启动恢复无效返回，精简任务合同和穷尽类型分支。
4. 收回 CLI 命令导入，复用现有边界用例覆盖实际入口。
5. 更新受影响的当前架构说明与 changelog，检查 diff、完成验证与代码审查。

### 验证与消融

- 复用 GitHub SDK、Projects 同步、结果恢复、线程恢复、triage、INITIAL 交付及启动恢复测试。
  关键场景为真实 HTTP 拒绝、408/超时、GraphQL 部分 mutation，以及先写入不确定再读取失败。
- 回执复用现有 INITIAL 与 retired 路径；缺失/重复的同一规则只保留足以覆盖不同后果的用例。
- 对照 `RevisionTask`、`Assignment` 的 JSON Schema 与序列化字段顺序；保留三种任务身份拒绝测试。
- 扩充现有独立解释器导入边界用例，验证 `app.cli` 不加载 control/persistence/psycopg/githubkit/bub；
  复用现有 CLI 命令行为测试。
- 最终运行 PostgreSQL 全量 pytest、`uv run ruff check app tests`、`uv run mypy app tests`、
  `uv build --no-sources` 与 `git diff --check`，依照 [CI](../../.github/workflows/ci.yml)。
- 错误证据修正涉及恢复判断：实现后、自动代码审查前，在独立进程恢复旧的状态码推断，
  同一 GraphQL 部分失败场景必须重新失败；脚本与输出置于本会话 `.cache/2026-10-12/`。
  其余改动删除无效层次，不新增状态机制，不另造消融实验。

### 未决问题与延后事项

实施前需要用户明确批准上述范围；没有额外产品选择阻塞方案。
本地 PostgreSQL 的运行状态与检查环境在实施时确认，不将旧计划报告视为本轮验证。
真实 GitHub App 在线调用、数据库迁移、部署修改和新增运行功能不属于本轮范围。

## 2. PR change timeline
