# M4 Feature 7/9：准备期只读 GitHub 故障重试

> 状态：用户已批准顺序独立实施并推送；实现与自动化验证完成；等待用户统一 review/合并。
> 规划日期：2026-09-09；基线：`main@origin` / `be65878f`（归档恢复修复 #27 已合并）。
> bookmark：`feat/m4-preparation-retry`；实现前置：F2；F6 若已合并需覆盖 CLOSED 边界。
> 本 PR 以 F6 bookmark 为 base；按用户指示无需等待合并，独立 review 后由用户统一合并。

## 1. 目标与依赖

依赖 F2 的独立 sweep。当前 prepare 中 PR/ref/Issue 读取临时失败会持久失败并要求显式 retry；
本项对尚未 Started、尚未发送 Assignment 的只读准备操作增加有界自动恢复。
已实现的 GitHub effects 重试、M3 原 worker 归档恢复继续沿用。

## 2. 方案

- 重试边界仅覆盖 control prepare 中明确列出的 GitHub 读取：Issue/repository/PR/ref、
  修订基线、讨论/线程/文件上下文。只读 GraphQL 可纳入；token 签发、CodeAgent、Judge、
  push、open PR、comment、resolve 和 worker 已开始执行都不纳入。
- 使用 `GitHubAdapterError` 分类：仅 RATE_LIMITED/TRANSIENT 自动重试；
  AUTHENTICATION/PERMISSION/NOT_FOUND/VALIDATION/CONFLICT 保留明确失败与手动恢复。
- job 增加独立的 prepare_failures、prepare_next_at、prepare_first_failed_at 和脱敏错误，
  不借用 revision_cycles 或 assignment attempts。默认最多 5 次连续失败、总窗口 15 分钟。
  成功准备后清除该准备期退避记录；超过任一上限进入现有失败路径。
- 下一次时间使用有上限指数退避并遵守 Retry-After；如果服务端要求的等待超出总窗口，
  停止自动重试并显示原因，不提前尝试。时间与计数持久化，重启不能重置预算。
- 读取失败且尚未派发时，按 assignment token/epoch 条件原子退回可排队状态并设置到期时间；
  scheduler 跳过未到期 job。Flow binding 仍归原 worker，不能转给其他 worker。
- 发出 Assignment 前的竞争必须可证明：Started/断线恢复/旧 token 已变化即不走自动退回。
  cancel、merge、F6 close 及未应用结果优先，重新准备前复核生命周期和冻结基线。
- 跨次读取重新构造完整候选快照，只在准备成功时按现有冻结规则提交；不能拼接不同重试轮的
  head/base/context。既有 SDK 有限读取重试应计入总准备时间，不形成无界嵌套重试。
- CLI 显示次数、下次时间和最后错误；显式 retry 沿用既有恢复路径，并继续拒绝 unknown executor。

## 3. 落点与实施顺序

| 落点 | 改动 |
|---|---|
| `app/control/coordinator.py`、`gateway.py` | 窄范围分类与未派发退回 |
| `app/models.py`、`store.py` | 准备期预算及到期领取 |
| `app/config.py`、`cli.py` | 上限配置与诊断 |
| 新增 `tests/test_preparation_retry.py`，既有 revision/gateway 测试 | 重启、次数和派发竞态 |

先写“503 后无需人工 retry 即恢复”的失败测试，再加入数据库到期条件；
接入 prepare 分类后验证 Started、合并和断线竞态，不增加 worker wire 字段。

## 4. 验收

- 暂时 503/429 恢复后只产生一次有效 Assignment，未 Started 的失败不消耗修订轮数。
- Retry-After、次数和总窗口跨 serve 重启不丢失；到期前其他 Flow 仍可运行。
- 永久错误立即可诊断；超过预算停在既有失败路径，显式 retry 不回放旧结果。
- Assignment 已发出或 Started/epoch 改变时不自动重排，不出现重复 agent 执行。
- 取消、合并、关闭和 head/base 变化发生在退避期间时，旧准备无法绕过新的事实。
- 不影响 M3 清理 result 已到达但 GitHub 回写未完成的恢复路径。

## 规模预算与拆分触发点

按相对合并基线的新增行 + 删除行估算：产品 420、测试 460、文档 100，合计约
**980 行**。数字是规划预算，不是已实现统计；共享测试替身、schema、格式调整也计入。
目标 800–1100 行，预计超过 1200 行先缩小/拆出独立可验收 feature，不压缩测试或代码行数凑数。
优先另拆：自动重跑 CodeAgent/Judge、unknown executor 接管和所有 GitHub 写操作重试改造。未完成必需正确性验收时不能仅以行数达标合并。

实施按“先失败测试→最小实现→针对性验证→完整检查”
推进；完整检查为实际 PostgreSQL 下 pytest、Ruff、Mypy 和 `uv build --no-sources`。

## 实现记录

gateway 使用 `prepare_for_assignment()` 明确区分准备读取和后续凭据签发。只读失败分类为
`PreparationReadFailure`；Store 在同一事务核对 worker/token/epoch、未 Started/无结果和生命周期，
写入预算与重排，或写入现有控制失败结果。领取跳过未到期 job，成功清除独立准备字段。
读取使用剩余窗口 timeout，超时只取消只读操作；过期恢复不会再发 API 请求。准备期 SDK 读取
禁用自身自动重试，原 F3 PR 状态读取接口的已报告问题不在本次返修范围。
线程/上下文 API 错误不再冻结成半份证据，整轮成功后同事务冻结候选；永久错误保留现有人工恢复。
已覆盖暂时失败恢复、重启、Retry-After/预算、旧 token/epoch/Started/结果拒绝重排、回滚、
其他 worker 进展、窗口中慢读取、半份上下文和真实 SDK 限流响应；真实外部 E2E 尚未验收。

验证：实际 PostgreSQL 完整套件 **627 passed**；Ruff、Mypy（106 个源文件）和 `uv build --no-sources` 通过。

## 阻塞与恢复返修

- 永久读取失败也经过原 worker/token/epoch、未 Started、未接受结果的事务围栏，旧请求不能失败新领取者。
- 准备预算耗尽保留最后一次脱敏端点、错误分类和请求诊断，便于修复凭据或服务后显式 retry。
- 关闭 SDK 透明重试后，结果核验也保存独立失败次数、Retry-After 期限和错误；重启遵守期限，到期继续原结果，不消耗准备预算或重复 worker 执行。
- CLI 展示结果核验的下次重试时间和原因。

返修验证：PostgreSQL 完整套件 646 passed；Ruff、Mypy、build 通过。

## 执行中开 PR 的读取恢复

`get_ref` 默认由持久调度处理重试；只有执行中的 `flow.open_pr` 明确启用 SDK 有界次数重试。临时 503/429 可在同一次工具调用内恢复，不再依赖模型重复调用；准备期和结果核验仍立即持久化 Retry-After。真实 SDK 回归覆盖失败后恢复和同 call_id 回放不再读取。

本轮验证：完整 PostgreSQL 654 passed；Ruff、Mypy（108 个源文件）、build 通过。
