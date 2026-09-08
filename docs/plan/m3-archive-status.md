# M3 Feature 3/3：归档状态回写与闭环验收

> 状态：已实施，483 项自动化通过；真实 M3 待验收。更新：2026-09-08。
> 实施授权：用户“做 PR #26”；基线：`main` / `1571643c`；bookmark：`feat/m3-archive-status`。
> 实现前置：[F1 #24](https://github.com/b1indsight/agentflow/pull/24)、[F2 工作区清理 #25](https://github.com/b1indsight/agentflow/pull/25)。
> F1/F2 已合并，本分支已更新到 main，并在同一 PR #26 实施。

## 1. 目标与边界

F1 可靠记录合并，F2 验证原 worker 已清理工作区。F3 让用户在关联 Issue 和 PR 看见完成
标签及归档说明，回写失败可恢复，全部完成后才宣布整个 Flow 成功并释放 worker。

实施前 ARCHIVE 的 ResultPlan 没有 GitHub effects；已有评论查询/marker/receipt 机制可复用，
现有 `set_labels()` 会替换整组标签，不能直接用于保留用户标签的完成状态更新。

本 feature 只新增必要的状态配置和增量标签能力，复用现有 ResultPlan、effect ledger、
inbox/ACK 与 F2 清理凭据；不再增加归档表、独立后台回写队列或新的结果应用框架。
不管理仓库 label 定义、不删除触发标签/远端 branch、不增加 Project 或 LLM 总结。

## 2. 用户可见结果

清理凭据核验通过后，对关联 Issue 和 PR 各执行：

- 添加 `done_label`（默认 `flow:done`），移除配置的 `in_review_label`。
- 保留其他用户/项目标签和触发标签 `flow`，避免归档回写触发取消。
- 发布一条固定模板的归档说明：Flow ID、关联 Issue/PR、合并 SHA/时间、工作区清理已确认。
  数据取自 F1 合并事实与 F2 清理凭据，不重新请求模型，不暴露本机路径或凭据。

部署文档要求预先建立配置使用的状态标签，`done_label` 不得与 trigger/in_review label
相同。缺少标签或权限报明确可恢复错误，不自动创建仓库标签。只对上线后的归档产生通知，
不批量给已有 SUCCEEDED Flow 补标签或评论。

## 3. 副作用与完成边界

1. 为 ArchiveResult 构造一次冻结的 ResultPlan，包含清理凭据和 Issue/PR 所需 effects。
   只在 F1/F2 身份与合并版本检查通过后进入此路径。
2. 新增增量标签 effect：只调用 add/remove API，查询只校验负责的标签，不读取后全量覆盖。
   标签已加/已移除时幂等完成；区分“目标 label 不存在”与 repo/权限错误。
   GitHub API 依据：[Labels API](https://docs.github.com/en/rest/issues/labels)。
3. 评论使用现有 CommentEffect 与 marker 查询；marker 固定为 Flow + PR + 目标 Issue/PR
   + archive 用途，不依赖 retry job ID。远端写入结果不确定时先查 receipt，查不清就 pending，
   不盲目重发。已完成一侧后另一侧失败，只继续未完成 effects。
4. 沿用现有持久恢复循环处理超时、429/5xx 与 serve 重启。永久权限/配置错误保留待处理
   ResultPlan 和 job.last_error，不把已清理结果降为未清理，不释放 binding。
5. 将 F2 的最终提交放在全部必需 effects 完成之后：同一事务完成 ARCHIVE、Flow SUCCEEDED、
   binding 释放与 result applied，再 ACK。worker 在等待时保留成功 journal，不再次执行清理。
6. 沿用 `flow retry` 对 pending effects 的现有模式，为 archive 添加有限分支：只重置可重试
   的失败 effects，沿用原 result/job，不创建新 ARCHIVE、attempt 或 INITIAL。F2 的清理失败
   retry 仍只处理清理未成功的情况，两种重试以已持久清理凭据区分。

Flow 在归档期间保持 ACTIVE，占用原 binding，现有同 Issue 活跃 Flow 约束继续生效。
不为提前释放 worker 再建立一条独立通知生命周期；回写长时间阻塞时通过 CLI 暴露原因。
`on_archived` 在完整结果提交后保持现有尽力调用和异常隔离，不承担必要标签/评论，不保证
跨崩溃 exactly-once 投递。M3 不扩展插件投递框架。

## 4. 落点与实施顺序

| 落点 | 改动 |
|---|---|
| `app/github_client.py`、`app/config.py` | 增量标签 API、done label 和冲突配置校验 |
| `app/result_models.py`、`control/effects.py` | 标签 effect、查询回执与重试 |
| `app/control/results.py`、`app/result_store.py` | Archive ResultPlan、稳定 marker、回写后完成 |
| `app/control/service.py`、`app/cli.py` | pending archive effects 的有限 retry 与错误显示 |
| README、架构/计划状态、GitHub App 配置说明 | 三个 feature 的实施事实、恢复操作和验收证据 |

审批后先补 adapter/效果恢复测试，再实现增量标签与归档 ResultPlan；接入 pending retry 后
做 PostgreSQL 事务/ACK 回归，最后执行真实 GitHub App/worker 闭环并更新文档。

## 5. 验收

- Issue/PR 各有完成标签和一条准确说明；用户标签及 `flow` 保留，无额外模型调用。
- 已有完成标签、无 in-review label、标签并发变化：操作只影响配置管理的状态标签。
- GitHub 写入后、receipt 落账前崩溃；评论查询不确定；429/5xx；永久权限错误；一侧成功
  另一侧失败：保存清理事实，只续跑缺失 effects，不盲目重复评论、不重删目录。
- serve 重启、结果/ACK 重放、最终事务回滚：只有全部 effects 完成才设成功和释放，
  重放不重复完成；插件异常不破坏归档。
- 补充配置缺失/冲突和历史 SUCCEEDED 不自动补通知的测试。使用真实 PostgreSQL 验证
  ResultPlan、回执和本地完成边界，外部 GitHub 用 adapter 替身注入故障。

实现完成运行 `uv run ruff check app tests`、`uv run mypy app tests`、完整 pytest
（配置 `FLOWTOOL_TEST_DATABASE_URL`，数据库用例不跳过）与 `uv build --no-sources`。
本计划阶段仅检查文档差异与链接，未运行产品测试。

真实 M3 验收在专用仓库使用真实 App 和原绑定 worker：创建 Flow PR → 用户合并 → 核对
合并事实、目录消失、Issue/PR 标签和各一条说明 → 启动下一 Flow 确认 worker 复用。
另覆盖一次重复 merge webhook、一次清理/回写边界重启和一次运行中修订与合并交错。
记录具体 PR、delivery、数据库/worker 证据；未完成时明确“自动化通过，真实 M3 待验收”。
M1/M2 的独立真实验收和同秒 webhook 重放缺陷继续保留在原状态文档。

## 6. 实施与验收记录（2026-09-08）

- `LabelEffect` 仅管理一个标签的存在状态，Issue/PR 各冻结 done、in-review 与评论三项 effect。
  adapter 使用 add/remove；添加前核对仓库定义，remove 404 复查目标标签，保留其他标签。
- 新增 `done_label`，三个 trigger/in-review/done 名称不能为空或重名（忽略大小写），避免归档
  意外移除触发标签。评论固定记录 Flow、Issue/PR、合并 SHA/UTC 时间和清理确认；marker 不含 job/digest。
- 准备、恢复和发送前核验 F2 身份；GitHub 永久拒绝保留清理凭据、原 job/attempt/binding。
  `flow retry` 复用交付 effect 重试路径，仅重置明确失败的操作；未知评论只查 marker。
- `flow show` 派生剩余回写数、首个未完成 effect 和错误；全部回写后沿用原子完成与 ACK。
  成功后重放和升级前冻结的空 effects 计划均不补通知。插件异常在完成后隔离。

自动化证据：

- `FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool@127.0.0.1:55419/postgres uv run pytest -q --tb=short`：
  **483 passed / 0 skipped**，包含真实 PostgreSQL、Git/jj 目录、worker 断线/ACK 重放和下一 Flow 复用。
- 新增测试覆盖标签并发变化、已有标签、缺失定义/404、配置冲突、部分回写与永久拒绝、429/503、
  响应丢失、评论落账前重启、真实连接池重建、未知评论、冻结配置和 marker、合并版本变化、历史计划及插件异常。
- `uv run ruff check app tests`、`uv run mypy app tests` 通过（90 文件）；
  `uv build --no-sources` 成功构建 sdist/wheel。
  完整 pytest 仅保留既有 Starlette 弃用告警。

真实验收：**自动化通过，真实 M3 待验收**。当前工作区没有 `.env`/App 私钥，环境也未配置
App ID、数据库与 worker 凭据；尚未提供专用验收仓库或已连接原 worker。
尚无真实 PR/delivery/worker 运行证据，未执行真实 GitHub 回写或人工合并。
待提供专用环境后按第 5 节覆盖人工合并→归档→下一 Flow、重复 merge、边界重启及修订/合并交错；
M1/M2 的真实验收待办与已知 webhook 问题保持原状态。
