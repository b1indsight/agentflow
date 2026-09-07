# M2 Feature 2/4：bub 语义决策与答复

> 状态：已实现并合并 [PR #20](https://github.com/b1indsight/agentflow/pull/20)，自动化通过；真实 provider 语义 smoke 待执行。
> 日期：2026-09-07 · 实现基线：`main` / `78b8bc38`（已合并 PR #19）· 文档核对：2026-09-08。
> Bookmark：`feat/m2-semantic-triage`；计划与实现使用同一 PR。
> 实现前置：F1 持久反馈批次与 settle（`feat/m2-feedback-batches`）；F3/F4 消费本 feature 的决策。

以下保留规划基线与方案；F1–F4 当前状态及已知缺陷见 [M2 状态与待办](../architecture/README.md#m2-状态与待办)。

## 1. 需求与规划时缺口

[M2 路线图](../architecture/roadmap.md)要求普通讨论先经过语义门，只有已形成可操作结论才启动改码；
问题得到答复，未决讨论等待补充，闲聊被消费而不重复触发。强信号的授权和路由由 F1 负责。

规划时 `BubJudge` 执行 `bub run <prompt>`，取 stdout 首尾大括号；无 timeout，非零退出后仍继续解析，
`confidence` 未限制范围。Coordinator 仅提供 repo/PR/Issue 编号作为上下文。
`TriageVerdict.answer` 被忽略，ANSWER 又调用模型，而 answer/wait/ignore 都缺少明确批次消费语义。

本地事实：`uv.lock` 与已安装包均为 bub 0.4.1；其 `builtin/cli.py:run` 默认会话为 `cli:local`，
stdout 为带 channel/chat 前缀的 outbound，框架可加载插件。现有 pre-0.4 注释不是运行契约。
此次只读核对不证明模型服务、凭据配置或无工具执行路径已经通过真实验证。

## 2. 范围与决策

- 保留 `Judge` 端口、worker 执行 Judge 和 control 执行 GitHub 副作用的边界。
- 在隔离子进程中封装 bub 的一次模型调用，返回版本化结构化 envelope；不依赖通用 CLI stdout
  的字符串截取。优先使用锁定版本的库入口和显式受控配置，不继承个人 CLI 会话或自动插件。
- 明确关闭模型可调用的 shell、文件写入和外部副作用工具；control 提供所需 PR 快照。
  这是一项需要契约测试证明的 adapter 行为，不能只在 prompt 中写“不要改码”。
- 每次 batch 判定使用独立会话，所有需要的历史由持久快照提供；不建立 bub 原生 GitHub channel。
- 沿用 `revise/wait/answer/ignore` 四类结果；结果、reason、open_points、模型/adapter 版本与
  batch/context digest 一起持久化，便于恢复与定位判断依据。
- 模型回复直接使用已生成且校验通过的 `verdict.answer`，作为持久 CommentEffect 发送。
  TRIAGE→ANSWER 不再二次调用模型。新路径不再产生 ANSWER worker job；旧 pending ANSWER
  必须按升级要求先处理，删除其不再使用的协议/端口路径时提升协议版本。

不实现通用聊天记忆、自动搜索互联网、工具型研究 agent、预算平台或 UI。真实改码/push 由 F3，
针对 inline 的回复和线程完成语义由 F4；本 feature 将答复发到 PR conversation，并引用来源反馈。

## 3. 冻结上下文与决策契约

F1 提供 `batch_id`、item/version 快照、触发原因和未决批次接续关系。本 feature 增加类型明确的
`ReviewContext`：Issue 标题/正文、PR 标题/正文、head/base SHA、改动文件与有限 patch、当前反馈、
前次 open_points 和尚未处理的相关讨论。GitHub 数据由 control 读取，Judge 不持有 GitHub 凭据。

新增 PR 文件/讨论的只读 adapter；分页取数并设置明确的文件数、字符数和总上下文大小上限。
保留来源 ID、kind、作者、时间、文件与原始 commit 信息；截断、二进制文件、API 缺失都显式标记。
远端上下文在网络调用后持久冻结，prepare 重试复用快照；本轮 head SHA 与快照绑定。
不把“未取得 CI”写成“CI 成功”；完整 CI 归因不在本 feature 的决策条件中。

所有 issue/review/patch 内容作为不可信数据块输入，系统指令与数据分隔并由结构化序列化转义。
反馈中的“忽略规则”“自行 resolve”不改变权限、阈值、状态机或可用工具；判定仅能返回契约字段。

| 判定 | 校验与本地行为 | 外部动作 |
|---|---|---|
| revise | confidence 有限且在 [0,1]，达到阈值，instruction 非空；持久派生一个带 batch_id 的 REVISION | 复用账本发送本轮修改说明；后续交付由 F3 处理 |
| 低置信度 revise | 降级 wait，保存 reason/open_points，保留反馈 | 不启动 Codex |
| answer | answer 非空且有快照依据；不能回答时返回 wait | 发送已校验答案，确认成功后消费本批次版本 |
| wait | 保存未决问题与原因，批次 waiting；新 generation 到来才再次判断 | 不周期发重复等待评论 |
| ignore | 明确消费本批次版本；不表示线程 resolved | 无 |
| 解析/超时/模型异常 | 可审计的 `wait`，区分 failure reason；保留输入 | 不派生 REVISION，不发送错误输出或空答案 |

verdict 使用严格枚举，拒绝 NaN/Infinity、额外字段、错误类型和不匹配的动作字段。
批次含互相矛盾或混合需求时，revise 指令明确列出有结论的修改，未决点保留；仅在整批确实可答复
或可忽略时消费整批。不能把一条问题的答案当作其他未处理修改诉求已被处理的证据。
缺失/截断内容若影响结论必须 wait，不能推测没有看到的代码事实。

答案副作用 key 使用 batch/source job 与固定用途，重放复用同一答案及 receipt。
取消、过期 attempt、关闭/合并 PR 或失效 batch 在发送前重新校验；在途请求按现有账本记录结果。
新反馈留到下一批，不混进已作出的答复或修改说明。worker result 仅回传决策，control 校验身份。

## 4. bub adapter 与故障处理

`app/judge.py` 保留薄的 Judge adapter，新增受控子进程入口 `app/judge_runner.py`：输入为一个
结构化请求，stdout 仅输出一个最终 envelope，stderr 承载有界诊断；模型原文不作为任意日志输出。
模型/provider/凭据通过 WorkerSettings 的明确配置映射传入，凭据只留在 worker 内存和子进程环境，
不进入 task/result、job payload、日志或 durable journal。

实施第一步对 bub 0.4.1 的配置、一次调用、最终消息、禁用工具/插件和会话/tape 隔离做契约试验。
选择能满足上述行为的最小库封装并固定兼容依赖范围；如果该版本无法实现，更新本计划说明具体
adapter 或依赖变更，再在同一 PR 完成审批，不能退回共享个人 CLI 配置或任意工具的模式。

父进程限制执行时长、stdout/stderr 字节数，超时或取消时终止并等待子进程退出。
启动失败、非零退出、空输出、多份 JSON、畸形 envelope、缺失模型配置分别产生明确诊断，统一
在领域层保守等待。一次 batch 一次有界调用，不通过自动重试循环耗费模型请求；新反馈可重新触发。
模型配置启动检查只验证配置完整性和 adapter 可用性，不在 health check 中发付费模型请求。

## 5. 文件布局与实施顺序

| 文件 | 责任 |
|---|---|
| `app/judge.py`、`app/judge_runner.py`（新增） | 受控 bub 调用、超时、envelope 与解析 |
| `app/models.py`、`app/protocol.py`、`app/ports.py` | 严格 verdict、快照类型、移除重复 ANSWER 模型调用 |
| `app/config.py`、`app/worker/runtime.py`、`app/worker/executor.py` | worker 模型配置、环境过滤与任务执行 |
| `app/github_client.py`、`app/github_models.py`、`app/context_builder.py` | 有界 GitHub 快照和数据边界 |
| `app/control/coordinator.py`、`app/control/results.py`、F1 `app/review_store.py` | 快照冻结、四类消费与派生规则 |
| `tests/test_judge.py`、`tests/test_review_context.py`（新增） | 契约、无工具、隔离、上下文与失败分类 |
| 既有 coordinator/protocol/GitHub/recovery/boundary 测试 | 派生、答案、精确消费与恢复 |

1. 先以本地受控 fake 模型端点完成 bub 契约试验与失败测试，不使用生产凭据。
2. 实现严格 verdict/envelope 和 worker 配置，将任何错误映射到可解释 wait。
3. 为真实 PR 快照的分页、截断、缺失数据和不可信内容编写测试，再接入 control prepare。
4. 接入四类持久决策和直接答案副作用，移除新流程中的重复模型调用。
5. 组合 F1 验证 waiting 接续、混合反馈、执行中追加反馈与结果重放；同步文档中的 Judge 状态。

## 6. 测试与验收

- 确定性 fixtures：明确修改、讨论未定、代码问题、闲聊、意见冲突、中英文混合和注入式文本；
  自动化验证契约和行为，不把固定假模型返回值当作真实模型语义准确率。
- 边界：阈值刚好相等/略低、负值/>1/NaN、空 instruction/answer、非法 verdict、超时、非零退出、
  带日志输出、重复 JSON、超大输出、缺配置、跨 Flow 会话与工具/凭据隔离。
- PostgreSQL：同 result 重放仅一个 revision/answer effect，answer 成功才消费，wait 重启后保留，
  下一条反馈触发重新判断；cancel/新版本不能被旧 result 覆盖。
- 运行 Ruff、Mypy、完整 pytest（含真实 PostgreSQL）与 `uv build --no-sources`；沿用 F1 的
  停机、清理旧 pending tasks/results 和协议版本升级要求。
- 单独记录真实 bub smoke：固定模型/版本、受控 PR 快照集、预期判定、实际输出、超时与失败；
  未配置真实 provider 时只能报告自动化通过、真实语义验收待执行，不能标记本 feature 已全面验证。

完成标准：普通反馈经校准 adapter 产生可恢复决策，答复不再二次生成或重复发布；
反馈来源、判断证据和未决事项均可追溯。

## 实施记录（2026-09-07）

- 固定 bub 0.4.1，使用其 `ModelRunner.completion_response` 单次入口，`tools=[]`、`hooks=None`、
  无 fallback、SDK `max_retries=0`。不创建 framework、加载插件或读写 tape；每次调用独立进程/
  请求 ID。假模型端点验证真实库的流式最终消息及单次请求，未使用生产凭据。
- `app/context_models.py` 定义证据类型，`app/review_context.py` 承担 control 专有快照构建，避免
  将 GitHub 读取混入 worker 共用的 Codex prompt builder。首次远端读取后在工作流锁内冻结；
  重启及 prepare 重试复用快照。文件 40 个，三类讨论各 20 条，当前反馈/历史问题各 60 条；
  单项正文/patch 4000 字符，快照 96000 JSON 字节。所有缺失/截断均采用保守 wait。
- 原模型结果保存在持久 result inbox；批次保存接受的决策、context digest、版本、reason/
  open_points。低于阈值的 revise 保存为 wait，原始建议仍可在 result inbox 查证。
- 协议 v6 移除 ANSWER task/result/Judge.answer；答案及来源直接进入原有 CommentEffect 账本。
  已发未知结果按 marker 恢复；确认后才消费精确版本。发送前检查取消、批次、attempt、PR 状态
  与 head/base，新反馈和新版本留到下一批。revise 的未决点继续保存供 F3/F4 消费。
- 验证覆盖严格数值/动作契约、隔离配置、无工具、重复/污染 JSON、非零退出、输出大小、启动失败、
  timeout/取消回收、分页、快照大小/缺失/读取期间 SHA 变化、等待接续、精确消费及 PostgreSQL 恢复。
- 审查修复：未开始执行的 TRIAGE 即使尚无上下文也可重启恢复；已开始却缺上下文的旧任务仍阻止启动。
  Judge 超限退出后持续排空 stdout/stderr，并将终止回收限制为 5 秒；真实子进程覆盖两条管道的持续输出。
- 无效反馈引用或 context digest 使当前 Flow 失败且不发布评论，支持显式 retry。发布前 head/base 变化
  则标记旧上下文过期；retry 用最新未消费反馈创建新批次并重取快照，保留旧快照和决策供审计。
- 上下文分页读取和 capability 准备期间续租；冻结前校验任务所有权与租约，派发前再次校验 token/epoch。
  PostgreSQL 用例覆盖过期、重新分配及超过初始租约的准备过程，避免旧准备结果覆盖或派发新分配的任务。
- 升级要求：旧 serve/worker 排空在途 review、处理 failed ANSWER 和所有未应用旧结果/journal，
  再一起升级。保留历史 `JobKind.ANSWER` 只供数据库读取，禁止恢复到已删除的执行协议。
- 真实语义 smoke：**未执行**。本机假模型测试只证明 adapter/控制流契约，不代表真实模型对中英文、
  矛盾讨论、注入式文本的语义准确率。本 feature 尚未全面语义验收；需部署时指定真实模型与受控快照集。
- 验证结果：Ruff、Mypy、完整 pytest **264 passed / 0 skipped**（真实 PostgreSQL）和
  `uv build --no-sources` 通过。保留既有 Starlette TestClient/httpx 弃用告警。
  当前环境未配置 `FLOWTOOL_JUDGE_MODEL` / `FLOWTOOL_JUDGE_API_KEY`；未调用真实付费 provider。
