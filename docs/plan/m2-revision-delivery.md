# M2 Feature 3/4：可靠修订与会话兜底

> 状态：待审批，plan only · 日期：2026-09-07 · 基线：`main` / `9fd98266`（PR #18）
> Bookmark：`feat/m2-revision-delivery`；本 PR 获批后继续承载实现。
> 实现前置：F1 `feat/m2-feedback-batches`、F2 `feat/m2-semantic-triage`。
> F4 `feat/m2-review-resolution` 在本 feature 确认交付后处理线程与完整 M2 验收。

## 1. 目标与当前问题

用户在原 PR 上提出修改，Flow 应复用独占 repo 和 Codex 会话完成修订，commit/push 到原 branch，
并在 control 确认 GitHub 远端提交后报告结果。正常情况下连续 resume；会话确实失效时允许一次
有边界的上下文重建，不新建 Flow、branch 或 PR。

当前代码已经有 `RevisionTask` 和 `CodeAgent.resume`，但仍有以下缺口：

- `assignment_secret()` 只给 INITIAL 签发 push capability；resume 不传 `CodeAgentContext`。
- worker 修订只看 exit/output，没有像 INITIAL 一样准备/校验 repo、验证 VCS backend、commit、
  clean working tree 和远端 branch；缺输出时会构造空 `CodexResult()` 当作正常结构。
- `build_revision_prompt()` 复用了首轮“push 后调用 flow.open_pr”的指令，而 REVISION 未启动该 broker。
- session 缺失直接失败，没有可分类的 resume 失败或重建策略；revision_cycles 在 started 时递增，
  需要证明 Started/结果重放不会重复消耗额度。
- clarification/deferred 后仍请求 re-review，addressed 即本地 resolved；后者由 F4 接管。

本 feature 将 M1 已验证的交付约束延伸到修订，并消费 F1/F2 的冻结批次与决策。
完整 rebase/冲突解决、人工 push 合并、跨 worker 迁移、运行中 kill、`/flow restart` 和预算系统
仍是后续工作；遇到不确定的工作区或远端状态明确保留反馈并暂停。

## 2. 任务与结果契约

RevisionTask 携带 Flow/attempt/batch、repo identity、原 PR 编号、稳定 head branch、base branch、
预期远端 head SHA、session/session generation、冻结 instruction/feedback/context，以及执行模式。
worker 使用既有 Flow binding；不能接受别的 Flow repo、不同 attempt 或过期 assignment。

RevisionResult 增加结构化交付事实：运行模式、原/新 session、VCS backend、branch、local commit、
remote SHA、working tree 校验结果、失败阶段/原因与严格 `CodexResult`。输出字段中的反馈标识使用
F1 的 item/version identity，并保留 GitHub kind/ID 供映射，不能只靠跨命名空间的数字 comment_id。
成功输出必须显式包含 summary、addressed、deferred、needs_clarification 与实际 VCS backend。

control 拒绝错误 Flow/attempt/batch、未声明的反馈、重复或互相冲突的 addressed/deferred 项、
不合法 schema 和缺少交付证据；LLM 自报 pushed=true 不是完成依据。
升级协议版本并同步 worker/control；继续使用现有 assignment token、epoch、terminal digest 与 journal。

## 3. 修订执行和交付顺序

1. control 读取并冻结 F1/F2 批次，确认 Flow active、delivery open、当前 worker binding 未释放、
   修订轮数未超限；查询实际 PR 的 head/base/状态与远端 ref，核对 delivery 的已知 head SHA。
2. control 为本次 REVISION 签发新的短时、单仓库 push capability；沿用 wire-only secret，
   不落数据库/task payload/journal。每次新的真实执行重新签发，结果重放不再次启动 Codex。
3. worker 验证独占 repo identity、目标 branch、预期基线与 clean tree；复用 M1 的 VCS helper，
   不执行硬 reset、自动丢弃改动或 force push。无法确认基线时在 CodeAgent 执行前失败。
4. 扩展 `CodeAgent.resume(..., context=...)`，让本轮进程拿到新 credential helper 环境与执行约束。
   实测所用 Codex CLI 的 resume 配置/会话继承规则，避免继承首轮已销毁的 MCP socket 或旧 token。
5. revision 专用 prompt 注入 repo 约定、issue/PR 快照、已知 head、批次反馈、前次未决问题；
   要求自主修改、测试、commit、普通 push 和结构化说明，不调用首轮 `flow.open_pr`。
6. worker 在退出后独立验证 backend、branch、commit、clean tree 和 remote SHA；非零退出、
   缺失/非法输出、未 push、脏树或 branch 错误均作为明确失败，不降级为空成功输出。
7. control 再查询 GitHub ref/PR，确认仍是同一张 open PR、同一 head/base、worker commit 与远端一致，
   并核对提交从冻结基线正常前进。将已核验交付事实冻结到 ResultPlan，随后执行必要副作用。
8. 通过既有原子结果入口提交 run/session、delivery head SHA、精确反馈处理状态与 job 终态；
   ACK 仅在必需副作用完成后发出。执行中到达的新反馈由 F1 留给下一批。

预检之后到 push 之间的远端变化无法用本地数据库锁消除；普通 push 的非 fast-forward 拒绝和
control 的后验校验共同防止把不一致状态报成功。代码中不承诺跨 GitHub/worker 的原子事务。
任何人工 push/force push、PR 已关闭/合并、base 改变或 lineage 无法证明的情况都保留事实并暂停，
不给线程 resolved，也不自动 rebase。通用对账仍属 M4。

允许明确 no-op：工作区干净、local/remote 均等于预期 SHA、输出说明现有代码为何已满足反馈。
no-op 不伪造新 commit 或“已推送修改”的通知；没有新的交付时不重新请求审核。
纯澄清结果可以保持原 SHA，但仍必须证明 repo/PR 状态一致。

## 4. session 兜底与恢复规则

优先 resume 同一 session；若当前 attempt 没有 session，或 adapter 能明确判断 resume 在开始改码前
因 session 不存在/损坏而拒绝，可在同一 revision job 内执行一次 `start`，cwd 仍为原 Flow repo。
重建 prompt 包含原始任务、PR/head、冻结批次、上一轮摘要和未决问题，不能假设记住旧对话。

| 情况 | 行为 |
|---|---|
| 正常 resume | 同一 session，新进程环境；成功后更新持久运行事实 |
| session 缺失，repo 干净且远端等于预期 | 允许一次 start fallback；记录原因和新 session |
| 已确认 resume 未开始改码的 session-not-found | 重新验证 repo/ref 后进行一次 start fallback |
| 超时、网络/鉴权错误、通用非零退出、解析失败 | 不自动 start；保留反馈，明确失败阶段 |
| session 失败后 repo 变脏或远端变化 | 不兜底重跑，不清理用户数据，等待人工处理 |
| fallback 也失败 | 本轮失败，不递归 fallback；保留完整诊断与反馈 |
| terminal result 已被 control 接收 | 恢复 result application，不重新运行 resume/start |

fallback 保持同 Flow/attempt/batch，增加 session generation 和两次 invocation 的审计记录；
只在新 session 结果被接受后更新当前 session。attempt 仍属于同一 Flow 的执行历史，delivery 不迁移。
worker 在执行前把 resume/fallback 阶段与一次性 fallback 消耗写入现有 journal 扩展；started、
fallback 启动前后、result 落盘与 ACK 丢失都要测试。进程重启后若无法证明某次执行未改码或未 push，
进入恢复待处理/失败状态，不用“没有收到结果”推断“可以安全再跑”。

修订额度以唯一 revision job 首次有效 Started 为单位幂等计数；同 job 的 fallback 和重放不重复计数。
现有同 Flow/同 attempt 的 retry 不重置计数；新 job 仍消耗额度。限制和 timeout 必须覆盖强信号直达。
沿用现有 retry 命令恢复明确失败的 job，但脏树、远端漂移等前置条件仍需人工处理后通过预检。

## 5. 反馈、clarification 与 re-review

- 未出现在结果中的反馈保留为未决，不能用“本轮成功”消费整个 attempt 的全部未 relay 项。
- addressed 仅说明 CodeAgent 声称处理完成；本 feature 保存该声明与核验 SHA，不标 GitHub resolved。
- deferred 保留原因；clarification 持久保存具体问题，发送一次询问后进入 F1 waiting。
  没有新反馈时不重复询问、不派生自动修订、不请求 re-review。
- 可以交付部分已完成修改并保留其他问题；clarification/deferred 阻止本轮整体完成通知和 re-review。
- 无未决问题、有已确认新提交时，复用现有标签/reviewer effect；F4 将线程回复/resolve 安排在其之前。
  reviewers 沿用配置与现有回退规则，不自动 merge 或把 CI 未运行描述成通过。

## 6. 文件布局与实施顺序

| 文件 | 改动 |
|---|---|
| `app/protocol.py`、`app/models.py`、`app/ports.py` | Revision task/result、反馈引用和 resume context |
| `app/codex_driver.py`、`app/context_builder.py` | resume 环境/错误分类、revision/reseed prompt |
| `app/worker/executor.py`、`app/worker/journal.py`、`app/vcs.py` | revision 准备、VCS 校验、一次性 fallback 恢复 |
| `app/control/coordinator.py`、`app/control/results.py`、`app/result_models.py` | 新 capability、远端核验、部分完成/等待、原子提交 |
| `app/github_client.py`、`app/github_models.py` | PR/head/提交关系的只读核验，复用既有认证和错误模型 |
| `app/store.py`、F1 `app/review_store.py` | 幂等轮数/session generation 与反馈结果持久化 |
| Codex/worker/VCS/protocol/coordinator/recovery 测试 | 模型行为边界、交付与故障恢复 |

1. 先用 fake CodeAgent 和真实临时 Git/jj repo 写失败回归：未 push、exit 0 无 schema、脏树、
   revision 没有凭据、session 不存在、新反馈被误消费。
2. 校准本机 Codex resume 的环境/CLI 契约，扩展端口和 assignment secret；分离 revision prompt。
3. 将首轮已有 repo/VCS 校验接到 revision，增加 control 的 PR/ref/lineage 核验。
4. 实现可分类且最多一次的 fallback、持久阶段和幂等轮数，验证重放/断连/重启。
5. 接入 addressed/deferred/clarification 与 waiting/re-review 规则；同步运行与升级文档。

## 7. 验收与限制

- 自动化覆盖 Git 与 jj 两种 backend 的正常新提交/no-op、错误 branch、未 push、脏树、远端漂移、
  PR closed/merged、部分完成、输出遗漏/伪造反馈 ID，以及旧 attempt/epoch/result。
- 安全边界复用 M1 测试：control secrets 不到 worker，push token 不进入日志/schema/任务/journal，
  resume 只拿本轮 capability；结果恢复不会新建 PR、重复 push 或重复消费修订额度。
- PostgreSQL 故障测试覆盖远端成功后本地提交失败、result/ACK 重放、cancel 与校验/副作用并发；
  等待问题时不重新请求审核，新回复继续下一批。
- 执行 Ruff、Mypy、完整 pytest（实际 PostgreSQL）、`uv build --no-sources`；协议/schema 升级沿用
  F1 维护窗口及旧 pending result 处理要求，不能猜测旧 result 缺失的 SHA/batch 字段。
- 真实 disposable repo smoke：完成 M1 PR 后连续两轮 resume，确认同 branch/PR/session、实际 SHA；
  再用可控缺失 session 场景验证一次 start fallback，保留版本/运行记录。外部 smoke 未执行须明确待验收。

本 feature 完成不等于已真实 resolve 线程，后者由 F4 验收。本 PR 当前只有计划，没有产品或测试代码。
