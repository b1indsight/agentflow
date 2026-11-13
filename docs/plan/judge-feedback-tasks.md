# Judge 反馈任务与相关上下文 — Feature record

保留 `answer / revise / wait / ignore`：answer 和 revise 都派发给 worker，前者仅回答，后者允许改码；Judge 只接收反馈关联的 PR 信息、讨论上下文和引用代码片段。

**Stage:** 实现与本地验证完成，正在审查并准备 PR；尚未部署。
**Baseline:** 已 fetch 的 `origin/main`，[`56cc629`](https://github.com/b1indsight/agentflow/commit/56cc6295c182d48ca25f92fb035893694efd1ed9)。
**Workspace / Bookmark / PR:** `judge-feedback-tasks` 隔离 workspace 位于 `.amp/in/judge-implementation`，从同步后的 `main@origin` 开始；交付 bookmark 为 `feat/judge-feedback-tasks`，本次审查通过后创建同名分支的实现 PR。共享检出的其他修改保持原样。
**需求来源:** [本线程](https://ampcode.com/threads/T-01a1d13c-5b4f-74c2-9355-d511e950aab6)最后确认的两项范围；早先合并 verdict 为 dispatch 的提议不采用。

## 当前有效方案

以下三项是 answer 的全部专用约束，以时间线 [001] 为准：

1. 任务调用显式要求 agent 仅回答、不修改代码。
2. worker 只检查执行前后本地提交 ID 无变化。
3. controller 只检查目标 PR 的 head SHA 与任务冻结值一致。

不增加 answer 专用的工作区干净、未跟踪文件、执行位置、远端地址或远端分支 ref 检查；不把“未改码”作为独立可证明的保证。原始计划中更重的 answer 核验与相应测试要求由 [001] 替代，其他内容（尤其反馈相关上下文）不变。下文初始计划保留为历史基线，不按其被替代部分实施。

## 1. Initial plan

### 背景与范围

当前 `JobCoordinator.triage()` 直接发布 Judge 生成的 answer，只有 revise 会创建 worker 任务；`build_review_context()` 遍历 PR 文件和各类讨论，任何 patch 截断都可能令 Judge 在模型调用前返回 `incomplete-context`。因此无关大文件可以阻断一个局部代码问题。

已使用 PR #146 的真实 diff 和已安装版本重放截断导致 WAIT 的分支；尚未读取该 Flow 的线上反馈批次，不能据此断言线上评论已经进入这个分支，更不能宣称本计划已修复线上问题。

本次仅做两项变更：

1. answer 与 revise 都创建已有 worker agent 任务，差别是是否允许修改代码；Judge 不再生成最终答案。
2. Judge 上下文按当前反馈相关性准备，仅包含相关 PR 信息、相关讨论和引用代码片段，不再默认收集整个 PR diff。

不增加 JobKind、调度器、检索服务、embedding、模型供应商或代码库语义搜索。不重设计重试、反馈消费、线程发布、自动 resolve 或 request-changes 强信号授权。普通反馈保留四种 verdict；混合批次需要改码时沿用 revise，同时交代其中的问题。wait/ignore 沿用现有语义。

### 变更一：answer 与 revise 复用同一任务链路

| Judge 决定 | 是否创建任务 | Agent 行为 |
| --- | --- | --- |
| `answer` | 创建已有 `JobKind.REVISION`，任务用途为 answer | 查代码、回答问题；不修改代码，不 commit/push，不直接操作 GitHub |
| `revise` | 创建已有 `JobKind.REVISION`，任务用途为 revise | 沿用修订、测试、提交、推送和结果核验 |
| `wait` | 不创建 | 保存理由与未决点，等待后续反馈 |
| `ignore` | 不创建 | 按现有规则消费相应反馈 |

**任务契约。** 在现有 `RevisionTask` 增加一个字段，不增加任务模型或结果模型：

- `purpose: Literal["answer", "revise"] = "revise"`：本次任务是否仅回答。默认 revise 只用于保持已存储旧修订任务的原有语义；新任务一律显式冻结 purpose。
- 入队时把 purpose 与 batch_id/instruction 一起保存，派发准备时写入冻结的 RevisionTask。强信号绕过 Judge 的既有修订任务明确使用 revise。
- `RevisionTaskResult` 和 `RevisionCodingOutput` 不增加 agent 自报的 purpose；controller 以冻结任务为准，不能由 agent 返回内容扩大权限。

**Judge 契约。** 保留现有 verdict、confidence、reason、instruction、open_points。新 answer 判定要求非空 instruction，描述需要回答的问题，不包含最终答案；revise 的 instruction 沿用现有用途。`TriageVerdict.answer` 仅为读取历史已保存答案保留，不再作为新 Judge 输出或新任务输入；在 `ReviewJudge.triage()` 的新模型响应校验处禁止旧式“直接给答案”的输出。历史记录的读取与新决策的校验必须区分，不能放宽新 answer 的要求来兼容旧数据。

**Agent 执行。** 保持现有 worker、workspace、原会话 resume、已证明未执行后的单次 fallback，以及相同的逐条 addressed/deferred 输出。`build_revision_prompt(task, repo_conventions)` 按 purpose 生成规则：answer 明确覆盖原编码会话里的改码指令，要求给出答案及代码依据；如果确实需要改码才能完成，报告 deferred，不自行升级为 revise。

**Answer 的完成核验。** 复用现有 Git/jj 准备和结果核验，在 answer 模式进一步要求：

- 执行前工作区符合原有干净基线；执行后仍干净，无新增未忽略文件。
- 本地提交、执行位置、远端地址及目标远端分支头保持冻结基线；新提交即使已推送且工作区干净，也不能作为 answer 成功。
- worker 与 controller 均按冻结任务核对“提交未变化”；检测到修改、提交偏移或远端偏移时，失败并保留现场和反馈，不发布成功答案，不 reset、删除文件或回滚远端。

这是基于现有 full-access agent 的行为要求与完成检查，**不宣称只读沙箱**；它不能证明临时写入、被忽略文件或其他外部副作用从未发生。本次不扩展凭据体系或重做隔离层，保留现有私有仓库/VCS 准备所需认证；不因为任务叫 answer 就声称 agent 没有写权限。

**结果发布。** Agent 在现有 addressed.note 中返回逐条答案，未答清的问题进入 deferred/needs_clarification。Controller 复用原线程映射、CommentEffect/ThreadReplyEffect、effect ledger、精确反馈版本消费和保守 resolve 规则。answer 不更新交付提交、不请求再次 code review；必要的结果摘要使用“问题答复”表述，避免显示为“修订交付”。不新增第二套答复发布器。新 answer 入队后，批次保持归属直至 worker 结果发布完成，不能入队即消费反馈。

### 变更二：只准备与反馈有关的证据

直接修改现有 `build_review_context(repo, flow, batch, pr_number)`，不增加平行 context builder。保留 `ReviewContext` 的结构和 SHA/digest 冻结机制，改变其内容选择：

| 内容 | 选择规则 |
| --- | --- |
| PR 信息 | 当前目标 PR 的身份、标题/正文、状态、分支、head/base SHA；沿用前后读取以检测基线变化 |
| 当前反馈 | 本批冻结的 item/version、作者、正文、来源、时间、未决点，不混入截止时间之后的新反馈 |
| 线程上下文 | 仅反馈所在的线程、根评论与相关回复；保留顺序、来源和版本，排除其他线程 |
| 引用代码片段 | 优先使用反馈已有 path、original_commit_id、diff_hunk；保留引用定位，不拼接该文件的整个 patch |
| 不再默认读取 | 全 PR 文件 diff、各类讨论的无关前缀、其他 PR 或原 Issue 的完整讨论；CI 继续为现有 not-fetched，不增加 CI 采集 |

反馈相关性使用确定的来源关系，不新增一次 LLM 判断：先利用已有反馈/线程映射；需要补齐时复用现有 ReviewThread/ThreadReader 分页接口，只把命中的线程放入上下文。为定位线程而扫描元数据不等于把全 PR 讨论传给 Judge；现有用于接收反馈的扫描器保留，不为缩小 Judge 输入而删除它们。线程快照与执行结果的回复映射继续共用原有来源，不另建缓存或索引。

非 inline 问题、未附代码的问题或无法解析的符号引用保留原文；例如“runtime.files 在哪里被使用”，可以判 answer 后交由 worker 定位。**没有现成片段本身不构成等待条件。** 对评论明确引用、且已能定位的代码片段，保持其来源 commit 与路径，不能用当前版本的无关代码替代。

仍保留有限字节/分页预算和完整性标记，但只对所选证据生效：未选择的文件、其他线程或无关 patch 再大也不产生 limitation；当前反馈或所需线程确实取不全、被截断，或者 PR 读取期间发生变化时，沿用现有保护。不得为了避免 WAIT 而清空所有 limitations，也不在此次计划中重做运行故障的分类和重试策略。

### 代码归属与接口

复用现有函数签名，除 RevisionTask.purpose 外不预设新核心类型、服务或公开方法：

| 模块 | 本次变更 |
| --- | --- |
| `app/domain/models.py`、`app/controler/review/judge.py` | 新 answer 表达任务要求；Judge 不产出最终答案；历史决策可读 |
| `app/contracts/protocol.py` | RevisionTask.purpose 及通信版本核对 |
| `app/controler/workflow/coordinator.py`、`app/persistence/reviews.py` | answer/revise 复用已有入队、冻结、批次归属；移除新决策的直接答复分支 |
| `app/controler/review/context.py`、`app/domain/context_models.py` | 在已有结构内缩小上下文来源，保留 digest 与来源关系 |
| `app/worker/execution/prompts.py`、`executor.py`、`vcs.py` | 模式化指令与 answer 的不变基线检查，复用原执行生命周期 |
| `app/controler/workflow/revisions.py`、`results.py`、`app/controler/review/resolution.py` | controller 复核 answer 未改码，复用发布与消费，仅适配必要的结果表述 |

只有当现有 GitHub 资源接口无法取得已定位线程的必要信息时，才在其原所属模块补最小读取能力；不预先增加包装层。

### 兼容、部署与边界

- 当前远端基线是 wire21。加入 answer 执行语义后，拟升级为 wire22；实施时如目标分支已占用该版本，使用下一个版本。旧 worker 必须在握手阶段被拒绝，不能把 answer 当成允许改码的普通 revision。
- 历史无 purpose 的 revision 任务只按 revise 解释；不改写历史任务的结果摘要或 digest，不把已有失败任务自动重跑。journal5 的业务结果结构不变，优先保持当前 journal 版本；实施时以真实读取合同验证，不预先承诺或增加 journal 迁移。
- 历史已发布 answer 不再发布，也不自动变成 worker 任务。升级前按现有维护流程检查并收敛旧 answer 的未确认发布；无法确认是否已经发送的答案，保留待核对，不能通过新任务重发。计划不新增排空脚本或历史反馈回填工具。
- 已冻结的旧上下文及历史决策保持可读，不原地重算 digest。新构建规则用于新批次；被旧上下文阻塞的存量批次由现有显式处理流程处置，不自动解冻或批量重跑。
- 实施仍需处理 schema 序列化、历史 decision 读取和正在发布的 effect 的兼容，不借机重设计工作流。部署/回滚前收敛在途任务与未确认副作用，再协调 serve/worker 版本；本计划不执行线上操作。
- 当前共享检出有大量其他未提交改动。实施前在同一功能的干净 bookmark/worktree 隔离；不覆盖、提交或顺带重构那些改动。基线的 task_content 是字符串，而当前检出含模型化改动，集成时保留最终目标分支的实际类型，不在本功能重做它。

### 方案比较与取舍

- **采用已有 revision 链路加 purpose。** 两类任务只在是否改码上有差异，共用原 worker、反馈引用、执行恢复和结果发布；新增 JobKind/AnswerTask/AnswerResult 会复制这些机制。
- **不只改 prompt。** revise 的默认执行规则明确要求 commit/push，answer 必须同时改变指令和完成核验，否则一次正常改码也可能被误报为回答成功。
- **不扩大全 PR 上限或直接忽略截断。** 前者仍会被更大无关 diff 阻断，后者会掩盖真实证据缺失；按来源关系选择证据才能满足本次范围。
- **保留线程策略、唯一上下文入口和既有认证。** 不额外改变自动 resolve，不另建 context builder，不重做只读凭据模型，避免把两项改动扩展为工作流和安全体系重构。

### 实施顺序

以下步骤只在计划明确批准后执行，整个功能使用一个 feature record、一个 bookmark 和一个 PR：

1. 对齐最新目标分支，确认在途/历史数据兼容点，补最小行为回归；先让 answer 仍直接发布、无关大 diff 阻断这两种旧行为在回归中失败。
2. 修改 Judge 新输出契约、RevisionTask.purpose 和双端协议；让 answer/revise 共用入队、冻结与原任务归属。
3. 修改 agent 指令、answer 基线检查和 controller 结果接纳，接入现有逐条答复与副作用恢复；保持 revise、wait、ignore 不变。
4. 直接收窄 build_review_context，只选择相关 PR/线程/片段；补齐目标线程分页与引用的最小支持。
5. 完成兼容与回归验证、两项简化对照，更新当前架构/运维说明和 CHANGELOG；实施后的代码审查仍按仓库流程进行。

### 验证与简化对照

优先扩展已有 `test_control_triage.py`、`test_review_context.py`、`test_worker_executor.py`、`test_revision_delivery.py`、`test_review_resolution.py`、`test_review_batches.py` 和 `test_protocol.py`，不为每个函数单独建测试文件。

- **回答闭环：** 同一代码问题被 Judge 判 answer 后只创建一个 worker 任务，入队时不发答案、不消费反馈；worker 给出带依据的逐条答案，controller 发布到正确线程，确认后才消费相应版本。重启/回执重放不重复发布。
- **禁止改码：** answer 的“正常回答且 SHA/工作区不变”通过；新增未提交改动、产生新提交但工作区干净、远端偏移分别不能通过。失败保留现场，不自动修复或改成 revise。使用真实临时 Git 仓库验证，并覆盖现有 jj 合同，不能只 mock 验证器返回值。
- **原行为控制：** 相同修改要求判 revise 后仍能交付新提交；wait/ignore、强信号修订、会话 fallback、取消及关闭/合并后拒绝迟到结果继续成立。
- **相关上下文：** 构造问题所在小片段与另一个超过旧限制的大 diff，仍能判 answer；选中的线程回复保留顺序与身份，其他线程不进入模型输入。不把“没有代码引用”或无关缺失判为快照不完整。
- **相关证据确实缺失：** 所选线程分页不完整或当前反馈被截断时保留 limitation，不能因为本次缩小上下文就照常生成无依据结论。以该场景作为“不应受影响”的对照。
- **兼容：** 旧 revision 缺少 purpose 仍按原语义读取；新 worker/controller 协议匹配；旧 answer/旧 context 读取不改 digest，不新建重复任务或评论。验证历史未确认发布的安全处理边界。

实施后、代码审查前做两项简化对照：暂时移除 answer 的“提交未变化”检查，应让“已提交且干净”的反例失败；暂时恢复全 PR diff 上下文，应让“相关小问题+无关大 diff”的回归失败。对照用于验证机制必要性，完成后恢复实现，不保留临时分支或双套代码。

运行项目既有 Ruff、Mypy、真实 PostgreSQL pytest 和 `uv build --no-sources`，先定向再扩大到受影响跨模块回归。Judge prompt 的真实模型效果另列为需明确批准的 eval：同一实际 Judge 模型，旧/新指令各使用全新会话，覆盖代码提问、明确修改要求、混合反馈、wait、ignore 和证据缺失控制，默认合计不超过 12 个会话；未经批准不执行，不把替身模型测试当成真实模型质量验证。

### 风险与待确认项

- answer 复用既有编码会话，会受旧“修改/提交”上下文影响；必须在当前任务中明确覆盖，并用基线检查拒收错误执行。现有 full-access 的限制保持如实记录。
- 范围缩小后，Judge 只需判断是否问答/改码，不应要求自己能回答所有代码事实；未引用的符号定位交给 worker。只有相关证据确实不完整时才保留原等待保护。
- 真实线上 Flow 6 的批次状态、历史 answer 在途数量未读取；属于部署前只读核对项，不阻止文档评审，也不授权线上写入。
- 本计划没有待用户补充的业务选择；需要用户明确批准后才能写实现或测试。若兼容核实要求超出上述最小变更，先更新本记录，不自行扩展范围。

### 依据与规划核对

源码依据为已读取的 `origin/main` 及当前检出对应入口：[现有 triage 与直接回答](../../app/controler/workflow/coordinator.py)、[任务契约](../../app/contracts/protocol.py)、[上下文准备](../../app/controler/review/context.py)、[Judge](../../app/controler/review/judge.py)、[worker 执行](../../app/worker/execution/executor.py)、[指令](../../app/worker/execution/prompts.py)、[结果核验](../../app/controler/workflow/revisions.py)、[结果应用](../../app/controler/workflow/results.py)、[线程回复](../../app/controler/review/resolution.py)。

[M2 semantic triage](m2-semantic-triage.md) 和 [control triage](control-triage.md) 是已交付历史记录，保留原文；本功能对其中“Judge 直接回答”和“全 PR 有界快照”的选择提出后续变更，不把历史记录改写为新计划。

计划草案通过本机 Claude Code 的 Opus 5.5 只读咨询生成建议；初始化确认模型为 `claude-opus-5-5`，tools/MCP 列表为空，调用成功。主线程核对源码后保留两项范围并删去超范围建议；这不代表产品代码、测试或线上运行已经验证。

## 2. PR change timeline

### [001] — 2026-11-13 — answer 只保留指令、本地提交与 PR head 三项约束

**需求与状态。** 用户明确要求缩小 answer 的约束：任务显式要求不修改代码；worker 检查本地提交无变化；controller 检查目标 PR 无变化。这项范围修订已确认；整体计划仍待实施批准，未修改产品代码或测试。

**决定。** 采用以下三项，替代初始计划中的“Answer 的完成核验”、controller 复核未改码的表述，以及涉及干净工作区、未提交文件、远端地址/位置不变的 answer 验收要求：

1. **调用指令：** `purpose=answer` 时显式要求“仅回答问题，不修改代码，不 commit/push”，覆盖同一会话中原有的修订要求。保留 agent 不直接发布 GitHub 评论的既有边界。
2. **Worker：** 在执行前记录本地当前提交 ID，执行结束后读取同一提交标识并比较。只在提交变化时触发本项拒收；不为 answer 增加工作区扫描、文件清单/哈希、未跟踪文件检测或远端核验。Git/jj 沿用各自现有提交标识的读取方式，不另建 VCS 核验框架。
3. **Controller：** 通过现有 PR 读取能力查询目标 PR 的 head SHA，与冻结任务的 expected_head_sha 比较；不一致则不接纳这次 answer。这里的“PR 无变化”仅指代码 head SHA，不包括标题、正文、评论或 review 的变化；不额外 fetch 分支 ref 或做提交祖先比较。

**接入边界。** answer 复用现有任务、恢复和结果发布，但不直接套用 revise 的 `clean_worktree`、本地/远端 SHA 相等及完整 VCS 交付检查；这些原有条件继续用于 revise。任务/批次/反馈版本身份、取消/关闭/合并保护及发布幂等仍沿用现有流程，不为 answer 再建一套检查。

**失败与限制。** 本地提交变化或 PR head 变化时，保留现场及未消费反馈，不自动回滚或升级为 revise。未提交的文件改动、先改后恢复、其他分支或外部副作用不在这三项检测范围内；提交未变不能作为“文件从未被修改”的证明，本次接受这一边界，不引入只读沙箱。

**实现与验证的同步修订。** 原实施顺序中“answer 基线检查”改为以上本地提交与 PR head 两个比较，不扩展执行器权限模型。最小验证场景为：

- answer 任务调用确实带有不改码要求；正常回答且本地提交/PR head 均不变可完成。
- 本地生成新提交，即使工作区干净，也不能通过 worker 的 answer 完成检查；不 reset 现场。
- PR head 与冻结值不一致，controller 不发布该次 answer；只有评论或 PR 正文变化不触发本项拒收。
- 只有未提交文件变化、本地提交及 PR head 均未变化时，不因新增的 answer 检查而拒收；不将这类情况测试成“已证明没有改码”。
- revise 的原有 VCS 核验及 wait/ignore 行为不变；相关上下文、大 diff 无关控制、精确线程回复与重放幂等的既定测试保留。

原简化对照中的提交检查反例继续使用“产生新提交但工作区干净”；不再增加移除工作区扫描等对照。反馈相关性上下文方案、purpose、历史数据兼容及协议升级范围不变。

**本次交付。** 仅更新计划文档与索引，并同步线程中的同一计划；未实现、未运行产品测试、未提交或推送。

### [002] — 2026-11-13 — answer 接入 worker 与现有发布恢复

**实现。** 用户要求先同步再实现后，fetch 确认目标基线未变化，在隔离 workspace 实施。answer/revise 均通过原 REVISION 入队并冻结 purpose；入队时不发答案、不消费反馈。新 Judge 输出只接受任务 instruction，历史最终答案仅从已保存决定恢复原发布，不转换成新任务。

**三项约束。** answer prompt 显式覆盖旧编码指令，要求只回答、不修改代码、不 commit/push。worker 仅比较本地当前提交：Git HEAD 或原生 jj `@`，后者使用 `--ignore-working-copy` 避免检查自身触发快照；session 明确拒绝后的 fallback 也先比较本地提交。controller 仅核对原 open PR 的冻结 head，元数据变化不阻止回答；revise 的既有工作区、分支和远端检查保持不变。

**发布与恢复取舍。** 原线程回复/resolve 以 worker 的交付提交判断 PR 是否仍有效，但 jj 工作提交本来就可以不同于 PR head。因此在内部持久 ResultPlan 冻结可信任务的 revision_purpose（历史缺省 revise），answer 发布及重放使用冻结 delivery head，不把 worker 本地提交当 PR head。沿用原效果账本、marker 和线程覆盖规则；answer 不更新交付 head/version，不请求 re-review。发布前 head 再变或回执待核对时，保留未消费反馈。

**兼容。** wire21 升为 wire22，避免旧 worker 把 answer 作为改码任务；journal5、结果结构和 inbox 版本不变。历史 context/decision/result digest 不重算；旧已保存直接答复按原 marker 恢复。升级仍须协调 serve/worker，先收敛在途任务和旧未确认答复。

**验证。** 真实临时 Git 覆盖正常回答、仅未提交改动、新建未推送提交及已推送提交；真实原生 jj 覆盖只读任务与 `jj new`。PostgreSQL 场景覆盖一次入队、逐条线程发布、PR 元数据/head/关闭、核验后 head 再变、回执重放和交付记录不变。相关测试通过。移除提交比较的内存对照使用 `unittest.mock.patch` 将 `app.worker.execution.executor.local_commit` 替换为固定结果，再运行 `test_revision_delivery.py::test_answer_checks_only_local_commit[push-False]`：测试因错误接受已推送提交而失败，确认本地比较不可省略；磁盘实现未改动。

### [003] — 2026-11-13 — 相关反馈上下文替代全 PR 收集

**实现。** 直接修改原 context builder；保留 PR 前后双读、冻结反馈身份/版本、未决点和反馈自带 path/original_commit_id/diff_hunk。不再读取 PR 文件 diff、原 Issue/task 正文或无关讨论前缀。先复用 batch.threads，必要时通过原线程分页定位 inline 来源；只有命中线程进入 Judge。非 inline 没有确定线程关系时保留冻结原文，不猜测讨论关联。

**预算与边界。** 线程扫描共享现有请求/字节预算；接口可能返回其他线程首批评论，但未命中正文不进入模型输入。所选线程缺失、分页不全、反馈截断和 PR 变化仍保留 limitations；没有现成片段不单独造成 WAIT。API 错误继续传播到准备重试边界，不冻结不完整快照。ReviewContext 结构及历史 digest 不变。

**验证与简化对照。** 上下文回归先在旧实现失败，再验证相关线程顺序、作者、截止时间、分页预算、API 失败、历史 JSON/digest 与无关大 patch。上下文子任务在内存绑定基线 `56cc629` 的旧 builder 后运行 `test_unrelated_large_patch_and_discussion_do_not_block_current_question`，再次因无关大 diff、讨论、Issue/task 产生 limitations 而失败；当前实现通过。保留来源相关性选择及所选证据完整性保护，不引入第二个 builder 或检索服务。

**最终验证与限制。** 完整真实 PostgreSQL pytest、`ruff check app tests`、`mypy app tests` 与 `uv build --no-sources` 均通过。测试从隔离 workspace 使用共享 `.venv/bin/python -m pytest -q --tb=short` 执行，独立数据库为本机端口 55449 的 judge_test；测试日志保存在 `.amp/in/artifacts/judge-feedback-pytest.log`，类型检查日志为 `.amp/in/artifacts/judge-feedback-mypy.log`。唯一 pytest warning 是现有 Starlette/httpx 弃用提醒。未执行真实 GitHub 写入、真实模型质量 eval、线上 Flow 6 重放或部署；本地模拟与回归不能作为线上回复已经恢复的证明。
