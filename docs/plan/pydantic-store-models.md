# Pydantic 持久化模型整合计划

> 状态：已批准，实施中 · 日期：2026-08-28 · 前置实现：PR #5 (`96de770f`)
>
> 本次保留 Psycopg 原生 SQL，不引入 SQLModel、SQLAlchemy ORM 或其他 ORM。

## 1. 背景与目标

PostgreSQL Store 当前先以 `dict_row` 取得字典，再由 `_row_to_flow()`、`_row_to_attempt()`、
`_row_to_job()` 和 `_row_to_item()` 手工构造 dataclass。项目已经依赖 Pydantic v2，且 Psycopg 3 的
`class_row()` 可以直接按查询列名构造带类型的模型，因此这层重复映射可以在不隐藏 SQL 的前提下移除。

本次目标：

- 仅把数据库持久化对象 `Flow`、`Attempt`、`Job`、`ReviewItem` 改为 Pydantic `BaseModel`；
- 查询使用 Psycopg `class_row(Model)` 直接返回对应模型，删除四个手工行转换函数；
- 保持模型当前可变语义、默认时间、枚举字段、结构相等和 Store 对外方法签名不变；
- 继续显式维护 SQL、事务、连接池、JSONB 适配、`SKIP LOCKED` claim 和 delivery 去重。

明确不在本次范围内：

- 引入 ORM、SQLModel、SQLAlchemy Session、关系映射或 lazy loading；
- 修改 PostgreSQL schema、配置、迁移方式、索引、事务或并发协议；
- 把 Store 改成 async，或处理 worker lease/fencing；
- 把 `SandboxPolicy`、`AgentRun` 等非持久化 dataclass 一并改写；
- 合并数据库模型、API 输入模型和跨 agent JSON 契约。

## 2. 实现方案

### 2.1 模型边界

- 四个持久化模型继承 `BaseModel`，字段、默认值和 docstring 保持现状；时间默认值改用 Pydantic
  `Field(default_factory=utcnow)`。
- 不启用 `frozen` 或赋值时重新验证，避免改变 Engine、Worker 和 CLI 当前就地更新状态的行为。
- 枚举和时间的数据库值由 Pydantic 在模型构造边界验证；发现不符合领域类型的数据库内容时直接失败，
  不静默生成半有效对象。

### 2.2 Psycopg 行映射

- 在返回领域对象的 cursor 上使用 `class_row(Flow)`、`class_row(Attempt)`、`class_row(Job)` 或
  `class_row(ReviewItem)`；查询仍列出或返回与模型字段一致的列。
- `record_event()` 等只关心是否返回一行的语句继续使用现有轻量结果，不为它们增加模型。
- 保留 pool 的 `dict_row` 默认值供非领域结果使用；不增加通用 repository、泛型 mapper 或 ORM 式抽象。

Psycopg 官方的 FastAPI/Pydantic 示例采用相同的 `class_row(PydanticModel)` 方式，并让类型检查器推断
`fetchall()` 的模型返回类型：
<https://www.psycopg.org/articles/2026/06/24/pydantic-fastapi/>

## 3. Change 与测试策略

审批后在本计划 change 后新增一个评审反馈子 change：

1. **`fix(store): use Pydantic row models`**
   - 先增加一个不依赖数据库的模型验证测试，证明字符串枚举和时间值能构造成正确的持久化模型；
   - 将四个持久化 dataclass 改为 Pydantic 模型；
   - 将 Store 返回路径改为 `class_row()`，删除手工 mapper；
   - 保留并运行现有 PostgreSQL 往返、共享可见性、并发 claim 和 delivery 去重测试。

验收要求：

1. Store 的公开返回类型和调用方行为不变；
2. PostgreSQL 返回的枚举、时间、JSONB 和布尔值能构造成正确的 Pydantic 模型；
3. `_row_to_flow()`、`_row_to_attempt()`、`_row_to_job()`、`_row_to_item()` 全部移除；
4. 本机无 PostgreSQL 时，非数据库测试通过、数据库集成测试按预期跳过；
5. CI PostgreSQL 上全部 pytest、服务 smoke、Ruff、Mypy 和 Build checks 通过。

## 4. 文档影响

这是内部持久化映射重构，不改变用户配置、命令、数据库 schema 或运行流程，因此 README、环境变量示例
和架构图无需修改。实施后仅更新：

- `docs/architecture/components.md`：说明持久化对象由 Pydantic 建模并通过 Psycopg `class_row()` 构造；
- `docs/plan/README.md` 与本计划：同步审批、实施和验证状态。

仓库没有 changelog 或文档生成器，本次不新增相应体系。

## 5. 风险与回退

- **行为漂移**：Pydantic 与 dataclass 的构造规则不同；通过现有调用方测试和代表性模型验证测试锁定行为。
- **查询列漂移**：`class_row()` 依赖列名和模型字段匹配；显式查询及真实 PostgreSQL 测试会在不匹配时失败。
- **过度耦合**：只转换已经与表结构一一对应的四个持久化对象，其他领域类型继续独立。

如出现不兼容，回退实现子 change 即恢复 dict-to-dataclass mapper，不影响已经完成的 PostgreSQL 替换。
