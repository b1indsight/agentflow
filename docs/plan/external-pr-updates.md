# 同分支外部更新 — Feature record

**Stage:** Planning，待用户批准；尚未修改产品代码或测试。
**PR:** 尚未创建；本次仅推送计划，draft PR 待另行授权。
**Bookmark:** `feat/external-pr-updates`。
**规划基线:** `main@origin`，`34a33153ded0aaa7afe553b90445c0dd044c3522`。

## 1. Initial plan

### Summary and motivation

允许协作者更新同一 PR 的来源分支。Flow 不再因为远端 SHA 与上次交付不同而进入 BLOCKED；每次编码前读取当前 PR，并把 Worker 工作区同步到该分支本轮 fetch 得到的提交。

当前 [reconcile](../../app/controler/reconciliation/pull_requests.py) 会因 head SHA 或 base 分支变化关闭 delivery 并暂停 Flow；[任务准备](../../app/controler/workflow/coordinator.py) 还会要求远端等于上次 delivery head；[Worker](../../app/worker/execution/vcs.py) 只验证本地与旧 SHA 一致，不拉取代码。必须一起调整这些入口。

用户要求原文：“允许外部更新，这里只要求是同一个分支就行，编码之前同步pr修订状态”。本计划按同仓库、同 PR、同来源分支识别目标；将同名分支的外部 rebase/force-push 也纳入支持范围，前提是不会丢弃 Worker 的未发布工作。PR 的目标分支使用编码准备时新读取的值。

### Goals, scope, and success criteria

- 外部更新后 delivery 保持 OPEN，不要求新 head 继承历史 delivery SHA，不自动新建 PR 或 INITIAL。
- 编码前刷新 PR 开闭、合并、来源仓库/分支、目标分支和 head 信息；本地 Git/jj 同步完成后才调用 CodingAgent。
- 上次已交付提交只用于证明哪些本地提交已发布，以及识别任务归属；本轮实际 fetch 的提交作为编码起点。最终结果必须等于远端，并相对本轮起点核验。
- 保留 worker/workspace/attempt/token/lease、反馈版本和结果回执的保护。脏工作区、未发布提交和未知执行不能因同步而被抹掉或重跑。
- 本项不自动整合编码期间的并发提交，不 force-push，不自动修复既有 BLOCKED Flow，不操作生产数据库或部署。

### Proposed behavior

例：上次交付 A，协作者把 `flow/6` 更新到 B。Controller 读取当前 PR；Worker fetch 得到 R（通常等于 B，也可能包含稍后更新），安全同步到 R，再开始编码。交付 C 必须相对 R 验证，不能继续要求 C 继承 A。

- 普通 push、merge/rebase 或同名来源分支改写：只要本地没有未发布工作，就使用新代码继续。
- PR 更换来源仓库、来源分支或 PR 身份：拒绝交付；closed/merged 继续执行现有生命周期逻辑。
- 准备阶段读到更新的 head：不把它当成旧基线冲突。PR 元数据与分支事实的瞬时读竞争按现有准备重试边界处理，不消费反馈。
- 已冻结的 Judge 上下文保留其 digest。需要刷新时清除旧上下文和决策后重建，不能替换 context 内的 PR 字段却复用旧 decision。明确 Request changes 的直接编码路径也必须读取新 PR。
- 编码启动后的远端更新可能使普通 push 或结果校验失败：保留现场和反馈，不自动重跑、不覆盖对方提交。
- 外部提交已经解决反馈时允许 no-op；是否产生新提交比较 C 与 R，不能比较 C 与 A。
- 正常关闭、外部更新后重开同一个 PR，旧执行和结果均已收敛时，也采用相同的来源分支身份规则；不能在 reopen 路径保留历史 head 限制。

### Technical design and affected modules

复用现有模型和执行信封，不新增同步 RPC、通用同步框架、独立发布证明 ref 或 JobState。

**任务与结果字段：**

- `RevisionTask.previous_head_sha: str`：新增；Controller 冻结任务时已有的已核验 delivery head，用来证明 Worker 本地旧提交已发布。它不要求等于当前远端。
- `RevisionTask.expected_head_sha: str`：保留；改为准备时新读取的 PR head，供 PR 快照和 answer 校验使用；revise 同步时不要求分支永久停在此 SHA。
- `RevisionTaskResult.baseline_sha: str | None = None`：新增；revise 实际同步后的本轮编码起点。成功的新版 revise 结果必须提供；不能拿最终 commit 代填。

**Controller：**

- [reconciliation/pull_requests.py](../../app/controler/reconciliation/pull_requests.py)：开放 PR 的暂停条件去掉历史 head/base 一致性，保留来源身份和生命周期检查；不在轮询中改写正在执行的 delivery head/version。
- [workflow/coordinator.py](../../app/controler/workflow/coordinator.py)：准备时重新读取原 PR 和分支事实，冻结最新 PR head/base 和 `previous_head_sha`。不要求远端等于旧 delivery，也不先把外部提交写成 Agent 的 delivery。继续在原 Flow 锁下复核 binding、任务和 delivery 版本，远端 I/O 留在锁外。
- [workflow/revisions.py](../../app/controler/workflow/revisions.py)：把当前共用的“准备准入”和“结果核验”分开。现有 `verify_revision_remote` 改为只核验结果，拟用签名 `async def verify_revision_remote(repo: Repository, flow: Flow, task: RevisionTask, result: RevisionTaskResult) -> str | None`。校验原 open PR/来源分支、最终 ref/commit 和结果相对 `baseline_sha` 的祖先关系；不再对历史 delivery 做远端祖先要求。
- [workflow/results.py](../../app/controler/workflow/results.py)：内部交付归属比较 `delivery.head_sha` 与 `task.previous_head_sha`；新版结果的起点、祖先关系、no-op 和新提交判定使用 `result.baseline_sha`。成功后才更新 delivery head，addressed 仍绑定实际结果提交及精确反馈版本。
- [workflow/lifecycle.py](../../app/controler/workflow/lifecycle.py)：同步调整已正常关闭 PR 的 reopen 身份判断；保留旧 executor/结果先收敛、失败现场不自动恢复和终态不重激活的要求。
- [workflow/effects.py](../../app/controler/workflow/effects.py) 与 [workflow/thread_effects.py](../../app/controler/workflow/thread_effects.py)：核查并同步使用本轮结果/当前冻结计划的事实，不让历史 SHA 限制从评论或 resolve 路径重新出现；最终效果仍检查实际当前 PR，不能发布过期“已修复”。

**Worker：**

- 修改现有签名为 `async def prepare_revision_context(repo_path: Path, branch: str, previous_head_sha: str, push_token: SecretStr | None = None) -> VcsExecutionContext`，保持同一职责和返回类型。
- 同步前验证原工作区、来源分支、原 remote 和干净状态。fetch 后，以原已交付提交与本次远端提交的可达历史证明本地不存在未发布提交；无法证明时不移动本地分支。
- Git 使用现有凭据环境和命令执行器 fetch 原分支，再安全移动干净工作区；禁止强推、自动 stash 和覆盖脏文件。外部非快进只影响本地同步，不要求它继承旧 delivery。
- colocated jj 使用原生 fetch/bookmark/工作提交操作，验证工作提交为空且锚定已知分支，保留其它本地修改和 change。不能把 jj 的空工作提交误判为未发布的编码成果。
- 从同步后的 `VcsExecutionContext` 捕获本轮 R；Agent prompt 明确区分 PR 读取快照和实际编码起点。fetch 是本轮代码快照的读取时点，不再为捕获后每次远端变化创建新 Job 或退款 revision budget。
- [executor.py](../../app/worker/execution/executor.py) 的 session fallback 使用同步完成后的 context 做只读复核，不能再次同步，从而掩盖被拒绝 resume 期间的本地变化。
- answer 继续遵守只回答、不修改代码的既有契约，不套用 revise 的工作区移动行为；它使用本轮新读取的 PR head。

**本次停滞的独立前置问题：**

用户提供的 traceback 指向 [_PullRequestRead](../../app/github/pull_requests.py) 要求 JSON 必须包含 `merge_commit_sha`。修正未合并 PR 的缺字段读取；`merged=True` 的完整合并证据仍须由 `PullRequestMerge` 严格验证。本计划纳入该修复，否则当前 Flow 在构建上下文时就会失败。

### Alternatives and rationale

- 只删 reconcile 判断：准备阶段和 Worker 仍会拒绝旧/新 SHA 不一致，不能满足需求。
- 每轮都把旧 delivery head 改成最新观察值，再强制 Worker 匹配该值：再次引入准备与 fetch 之间的 SHA 竞争，还会混淆已核验交付与远端观察。
- 只允许快进：不能覆盖同名分支外部改写；用已交付历史证明本地工作是否可安全移动更直接。
- 新增发布证明 ref、同步握手、preflight 重排队及修订额度退款：现有冻结任务与本轮结果已能记录所需的两个起点，暂不引入这些状态。

### Costs, risks, and mitigations

跨 Controller/Worker 修改协议；主要风险是把外部提交误归为 Agent 新交付、同步丢失本地未发布工作，以及 jj 空工作提交处理错误。通过分别记录历史已交付位置和本轮实际起点、真实本地远端测试及既有回执保护降低风险。

最新 PR 读取与分支 fetch 无法对远端提供原子锁。明确以 fetch 得到的 R 为本轮快照；之后的并发写由非强制 push、结果校验和效果发布前校验处理，不承诺自动合并在途冲突。无法证明本地历史安全时仍须人工处理，但不能仅凭远端与旧 SHA 不同就暂停。

### Implementation order

1. 在既有 GitHub 读取测试中复现缺字段失败，再修复未合并 PR 读取，保留合并证据负向测试。
2. 用既有 Controller 与真实 Git/bare remote 用例复现外部 push 后阻塞和 Worker 未同步的行为，再覆盖任务的新旧起点。
3. 实现 Worker 原生 Git/jj 安全同步、实际基线记录和 fallback 只读复核。
4. 接入 Controller 准备、结果、reconcile、reopen 与 effect 的相应规则，扩展既有回归。
5. 更新 [review-loop](../architecture/review-loop.md)；保留 [M4 原计划](m4-pr-lifecycle.md) 的历史内容，追加说明本项取代人工更新暂停规则。

协议版本随字段和语义一起升级，Controller 与 Worker 协调发布，不增加双协议运行模式。为历史持久任务/结果提供明确的读取兼容：旧记录仍按旧任务冻结值解释，不能为新版实时结果静默补造 `baseline_sha`。上线前先让旧在途执行和未决回执收敛；未知执行不能借升级重跑。回滚同样先收敛新版任务，不让旧 Worker 接收新语义。没有数据库列迁移计划；不自动解封旧 BLOCKED Flow。

### Validation and ablation strategy

以下均为待执行验收，不是已经通过的测试。

- 在未改代码上确认“外部 push → Flow BLOCKED”和“任务有新 head、Worker 仍留旧代码”的回归场景失败；修改后同一场景应在新代码上执行 Agent。
- 用本地 bare remote 和独立协作者 clone 覆盖普通 push、merge/rebase、force-push。用不同文件修改证明 Agent 实际读到了外部代码，且最终提交包含双方改动或合法继承新远端历史。
- 覆盖外部已修复后的 no-op、新提交识别，以及最终提交继承 R 但不继承 A 的非快进情况。
- 负向检查 dirty/untracked 文件、未发布 commit、错误来源仓库/分支、close/merge、编码中并发 push 和 session 拒绝后的修改；断言文件/提交保留、无误 ACK、无未知执行重跑。
- PostgreSQL 场景覆盖任务冻结/结果/效果重放，旧回执不能更新新轮次；reopen 后也不能因历史 head 不同卡住。
- 使用真实 Git 和 jj 执行同步测试；执行历史协议记录兼容、新旧握手拒绝及已有 answer/fallback 回归。
- 简化对比在实现后、代码 review 前进行：只去掉 reconcile 限制、再去掉 Worker 同步、再去掉本地已发布历史校验，分别验证是否在准备阶段失败、读到旧代码或丢弃未发布工作。保留有具体反例支撑的机制，不为比较提交临时测试或脚本。
- 完整检查按现有 CI：`uv run ruff check app tests`、`uv run mypy app tests`、有 PostgreSQL 的 `uv run pytest`、`uv build --no-sources`。不触发远端 workflow 或生产部署。

### Open questions and deferred work

审批确认本计划的同名分支外部 rebase/force-push 范围，以及目标分支采用新读取值的解释；推荐均按上述范围实施。没有其它必须由用户决定的技术问题。

实现时确认 Git/jj 的具体同步命令和历史 JSON 兼容落点，用真实本地集成测试决定细节。外部更新期间自动 rebase/cherry-pick 在途成果、跨 worker 迁移和旧 BLOCKED Flow 批量恢复均不在本项范围。

## 2. PR change timeline
