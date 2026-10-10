# Revision 结果契约去掉中间父类 — Feature record

**Stage:** Implemented — 实现及本地验证完成。
**PR:** [#72](https://github.com/b1indsight/agentflow/pull/72)。
**Bookmark:** `refactor/revision-result-contract`
**基线:** `main` / `a7c91202`（PR #68），2026-10-10。

## 1. Initial plan

### 背景、目标与范围

`ReviewResult` 只声明 batch_id 和 feedback_refs，唯一使用者是继承它的
`RevisionTaskResult`。当前仓库没有独立 ReviewResult 的构造、参数或消费入口。
依据：[结果协议](../../app/contracts/protocol.py)。
目标是让当前实际返回的 RevisionTaskResult 直接拥有自己的字段，减少一个没有独立
职责的类型。尽管改动很小，按用户要求单独建立计划 PR。

### 拟议行为与技术方案

将 batch_id、feedback_refs 原样移入 RevisionTaskResult，直接继承 ProtocolModel，
删除 ReviewResult。batch_id 的 gt=0、字段必需性、feedback_refs 类型及现有序列化
顺序保持一致；其余成功、失败、invocations 和 VCS 证据字段不变。
ExecutionResult 判别联合、Succeeded 包装、解析器及协议版本不变。
不增加替代父类、公共 FeedbackResult 或兼容别名。

### 备选、风险与兼容

保留父类可为假设中的其他 review result 提供扩展点，但当前没有使用场景。
选择直接声明，代价是未来若出现真实共享契约需重新评估。
主要风险是搬移时漏掉字段约束或改变 schema/序列化。此父类未被仓库其他代码使用；
不为未知 Python 外部消费者保留死别名。JSON wire 保持兼容，无数据库迁移。

### 实施顺序与验收

1. 实施前用代表性 RevisionTaskResult 记录 JSON dump 和 validation schema 的差异基准；
   检查基类名称等元数据差异是否影响实际 wire 契约。
2. 原样搬移字段并删除父类，搜索生产代码和测试，确保没有遗留引用。
3. 运行 `test_protocol`、revision 结果应用/恢复相关回归，以及 Ruff、Mypy、
   `uv build` 和 diff 检查。若仓库必需检查包含全套数据库回归，同样完成。

此改动只删除一层继承，不引入新行为，不新增镜像结构测试；复用现有合法/非法
revision 解析与结果恢复用例。无需机制消融，用相同输入比较前后 schema 与 JSON。
验收为只有一个实际结果类型，字段约束、wire 输出和业务结果保持一致。

### 开放问题与后续工作

无阻塞问题。其他结果模型、泛化抽象和全面 schema 重构不属于本 PR；四个计划可独立
实施与合并，本项可先完成。

## 2. PR change timeline

### 2026-10-10 — 实际结果类型直接拥有反馈字段

- **实现：** `RevisionTaskResult` 直接继承 `ProtocolModel`，在原字段顺序位置声明
  `batch_id: int = Field(gt=0)` 和 `feedback_refs: list[FeedbackRef]`，删除
  `ReviewResult`。生产代码和测试无遗留引用，未增加兼容别名或替代抽象。
  更新 CHANGELOG；本 PR 更新到已包含 #69、#70、#71 的最新 main `8610b43d`。
- **兼容性证据：** 修改前保存 validation/serialization 两种模式下的
  `RevisionTaskResult`、`Succeeded`、`ExecutionResult` 共 6 份 schema；用最小结果、
  成功 resume、双 invocation fallback 和执行失败 4 组输入比较结果 JSON、完整消息
  JSON、解析往返及 terminal digest。另比较 9 组校验错误，覆盖 batch_id 正数约束、
  两个字段必需性、feedback_refs 类型/元素约束、extra forbid 和 invocation 上限。
  修改前后全部一致，schema 元数据也无变化，协议版本仍为 v13。
- **实验与取舍：** 本次只移除一层继承，没有增加需要消融的机制。采用上述同输入
  前后对照，保留既有业务回归，不新增镜像结构测试。比较脚本和基线位于仓库本地
  `.cache/2026-10-10/pr72-contract-comparison.py` 与 `pr72-contract-baseline.json`，
  不进入版本控制。在本次独立工作区中复现：

  ```bash
  # capture 在修改前运行；compare 在修改后运行。
  ../../../.venv/bin/python ../pr72-contract-comparison.py capture
  ../../../.venv/bin/python ../pr72-contract-comparison.py compare
  FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool@127.0.0.1:55469/postgres \
    ../../../.venv/bin/python -m pytest -q --tb=short
  ../../../.venv/bin/ruff check app tests
  ../../../.venv/bin/mypy app tests
  uv build --no-sources
  ```

- **验证结果：** 实现前协议/修订交付/恢复/coordinator 基线 71 passed；实现后完整
  PostgreSQL 回归 **844 passed，0 skipped**。Ruff、Mypy（160 个源文件）、sdist/wheel
  构建和 diff 空白检查通过。测试有 1 条现有 Starlette/httpx 弃用提示。
