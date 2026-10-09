# Revision 结果契约去掉中间父类 — Feature record

**Stage:** Planning — 待审阅，尚未实施。
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
