# Changelog

## Unreleased

- M4 F4：Issue/PR 评论和 Review 正文按有界页持久对账，页内容与 continuation 原子提交；
  周期复扫捕获编辑，查询前冻结版本序号避免覆盖新 webhook。旧命令不执行、不补造强信号授权，
  页面缺席不推断删除。`flow show` 展示各来源的进度、失败和下次重试。
  返修限制单批输入并自动推进积压，保留授权提交信号与排队编辑；命令编辑失效旧意见且允许后续恢复。

- M4 F3：持久 PR 对账补偿漏失的合并 webhook，复用同一合并事务和归档入口；保存检查
  deadline/成功时间/错误，支持重启、分页、有限并发和 Retry-After。旧快照不能回退新状态，
  历史终态 Flow 只记录合并事实；`flow show` 可查看对账诊断。
  返修关闭 PR 读取的 SDK 内部重试，保证限流 deadline 先持久化。

- M4 F2：lease、settle 与网络结果恢复分别调度，结果按有界页和固定并发恢复；持续新增结果
  不会阻止旧 pending 结果再次被扫描。保留同 job 的实时/恢复排他和原 worker fencing。
  新增结果恢复并发与批次配置，默认 4 / 64；shutdown 等待恢复池退出。

- M4 F1：同秒远端观察与迟到 webhook 的重复转换按版本历史核对；已出现过的转换及无法
  定序的多后继编辑保留待核对，不再在正文回退后复活旧反馈。新鲜观察可继续推进，较晚
  时间戳的真实编辑不受影响；覆盖重启、回滚、跨连接并发和删除终态。
  返修补齐观察跳过中间版本和直接 webhook 冲突后继的覆盖。

- 修复归档评论发送前的本地认证失败被误判为未知结果：恢复凭据后可重试原结果，已有不确定
  发送记录仍仅查回执。状态写入停用 SDK 内部重试，保留读取（含标签预检、只读 GraphQL）和 push token 签发的有限重试；
  resolve 成功后的确认查询失败保留待确认记录，持久恢复遵守 Retry-After 或限流重置时间。
  重试资格校验归回 control service，归档等待检查改为数据库存在性查询。
- M3 F3：清理核验后，对 Issue/PR 增量增加完成标签、移除 in-review 标签，并各发布稳定 marker
  的固定归档说明；保留用户与触发标签，不创建仓库 label。新增 `FLOWTOOL_DONE_LABEL` 及配置冲突校验。
- 归档 GitHub 回写复用冻结 ResultPlan 和 effect 回执：响应丢失先查询，部分成功只续跑缺失操作；
  永久错误保留 ACTIVE Flow、清理凭据和 binding。`flow retry` 仅重置原结果的明确失败回写，
  `flow show` 显示清理与回写进度；全部回写后才原子成功、释放 worker 并 ACK。
- F3 自动化覆盖 PostgreSQL、重启/回执恢复、幂等标签和评论、配置与插件异常；真实 App/worker
  的完整 M3 闭环待验收，记录见 `docs/plan/m3-archive-status.md`。

- 修复 M3 F2 的任务交接恢复：未冻结的归档领取不再阻断 serve 重启；冻结命令在 Started 丢失
  或 grace 到期时保留原 token，核对冻结身份后恢复执行/结果。重连前持久保存已完成的 executor 结果，
  拒绝未启动编码任务时保留 root/Flow 归属，过期编码队列项不再阻止归档 retry。
- M3 F2：协议 v8 的 ARCHIVE 冻结 Flow/attempt、原 worker/repo identity 和 delivery version，
  control 核验 removed/already_absent 凭据后，原子提交 job、Flow SUCCEEDED、binding 释放和结果应用。
- worker journal v2 保留任务间的 root/Flow 归属，删除前持久化校验标记；只清理受管 repo 与
  `.partial`，拒绝 symlink/路径逃逸。部分删除和删除后崩溃可在原 root 恢复，终态和丢失 ACK 只重放。
- MERGED Flow 在归档失败、断线和恢复超时后保留原绑定；未知旧 executor 阻塞清理。
  `flow retry` 仅重试 ACTIVE 且已结束的失败 ARCHIVE，合并后 cancel 拒绝释放资源；
  `flow show` 展示派生归档进度，active 列表排除 SUCCEEDED。旧协议/journal 须排空或人工核验。
- F2 已覆盖真实 Git/jj 目录、PostgreSQL 原子回滚、断线/ACK 重放与下一 Flow 复用；
  GitHub 标签、评论及完整 M3 验收留给 F3。

- M3 F1：merged webhook 去重、合并时间/SHA、delivery version、旧工作退休和 ARCHIVE 入队
  共用 PostgreSQL 事务；重复事件只推进一次，合法关联合并不受反馈 bot/触发 allowlist 过滤。
- 合并后的任务准备、派发、flow.open_pr 和 GitHub 写入检查 delivery；迟到 INITIAL/REVISION
  保留运行事实，旧结果不改回 OPEN、不将归档 Flow 置 FAILED、不派生修订或消费反馈。
- 已合并 PR 的编码/review 重试在恢复 Flow 前被拒绝，避免终态 Flow 卡在 ACTIVE 并占用
  Issue 的活跃名额；失败 ARCHIVE 按 F2 的原 worker/ACTIVE Flow 条件重试。
- 已派发的 GitHub 操作只查询回执，不重发；ARCHIVE 等待旧执行和未应用结果收尾。

- 文档状态核对（2026-09-08）：M2 F1–F4 已通过 PR #19–#22 合并，真实 GitHub App/Codex/bub
  验收仍待完成。同步架构、路线图、可视化和计划记录；合并与 CI 证据见 [M2 状态与待办](docs/architecture/README.md#m2-状态与待办)。
- 记录尚未修复的 Medium：远端已观察 A→B 后，迟到的重复 A→B webhook 可能排队，在同秒 B→A
  回退后重放旧内容；详见 [F4 已知问题](docs/plan/m2-review-resolution.md#同秒重复-webhook-重放medium未修复)。

- 修复线程审查边界：同秒远端刷新按 item/version 校验冻结基线，并在同一事务中继续处理已排队的编辑；
  标签和 reviewer 请求发送前再次检查待处理反馈，发现新输入时保存诊断并跳过通知。
- resolve effect 只保存所属线程的 inline 处理依据，避免多线程重复保存整个 PR 的反馈历史。
- M2 F4：类型化线程/comment 快照、独立 cursor 分页和共享请求/字节预算；原 review 线程合并回复
  addressed/deferred/可关联问题，PR conversation 说明引用精确来源及已核验提交。
- 先确认回复，再检查完整人类反馈覆盖、本地版本与当前 PR/head，resolve 后复读；新评论和编辑
  幂等入下一批，远端查询落后于同秒 webhook 时不覆盖新版本。addressed 依据与远端 resolved 分开持久保存。
- 线程回复和 resolve 复用持久 effect ledger，响应丢失、重启、取消和数据库提交失败不重复写入；
  权限/映射问题保留诊断，暂停 re-review。旧 pending revision plans 须先核对，wire protocol 保持 v7。
- F4 自动化使用真实 PostgreSQL 与 GitHub 替身；真实 GitHub App/Codex/bub 完整矩阵仍待验收。

- 修复已 push 修订遇到通知永久拒绝后无法恢复的问题：保留核验结果与成功回执，显式 retry 仅补发
  被明确拒绝的通知，不重跑修订或增加轮数；状态不明的通知仍禁止盲目重发。
- 修订准备阶段的 PR/ref/Issue GitHub 错误进入持久失败及显式重试路径；总时限触发 VCS 取消时
  回收并等待整个子进程组退出，避免与后续修订重叠。
- M2 F3：冻结修订的 Flow/attempt/batch、PR/branch/head 与 session generation，每轮签发临时单仓库
  push capability；revision prompt、resume 环境与初始 MCP broker 分离。
- Git/jj 修订独立核验 branch、clean tree、本地/远端 SHA；control 验证原 open PR、仓库与 fast-forward
  lineage，原子提交 session、delivery head、精确反馈与 job 终态。no-op、deferred/clarification 不触发 re-review。
- session 缺失或明确的执行前拒绝允许一次 start；journal 持久记录阶段和调用，超时/不确定执行不重跑。
  Started 与修订额度同事务计数；失败保留 worker binding，结果重放和 fallback 不重复计数。
- 协议升级到 v7，旧未确认结果和已开始但缺冻结 assignment 的修订须先核对；同步 serve/worker 升级。
  完整 PostgreSQL/Git/jj 自动化已覆盖；真实 GitHub/Codex 多轮 smoke 与 F4 thread resolve 仍待验收。
- M2 F2：bub 0.4.1 受控单次调用、无工具/插件/共享会话，配置与凭据只进入 worker 子进程；
  timeout、输出上限、严格 verdict/envelope 与失败分类均有自动化覆盖。
- control 冻结有界 Issue/PR/patch/讨论快照，绑定 head/base SHA 与 context digest；缺失/截断保守 wait。
- answer 直接发布已校验答案及来源，取消、旧 attempt 和变化的 PR 在发送前拦截；持久回执后精确消费，
  不再创建 ANSWER worker job 或二次调用模型。reason/open_points 和模型/adapter 信息随批次持久保存。
- 修复上下文冻结前重启被误判为旧任务，以及 Judge 输出超限后管道阻塞导致回收挂起的问题。
- 无效决策引用或摘要使 Flow 进入可重试失败状态；head/base 变化后 retry 重建上下文并保留旧批次审计。
- 上下文和 capability 准备期间续租，冻结与派发前校验任务所有权，防止过期分配派发或覆盖新任务。
- 协议升级至 v6：serve/worker 同步升级，旧 ANSWER 任务和未应用旧协议结果须先处理；真实 provider
  的语义 smoke 尚未执行，假模型端点验证的是 adapter 契约和控制流。
- M2 F1：反馈版本、settle deadline 和冻结批次持久化；事件交接、批次任务派生及结果消费原子提交。
- 执行中的新评论/编辑保留到下一批；wait 等待新反馈，Request Changes 经授权与 grace 合批后直达修订。
- 修订/答复只确认明确的 item/version，取消和 retry 保持批次所有权，addressed 不再冒充远端 resolved。
- 协议升级至 v5：serve/worker 同步升级；旧在途及仍可重试的失败 review job、未确认结果须先处理。
- 修复恢复超时留下排队任务、retry 选中历史失败，以及升级漏查可重试旧任务的问题。
- ANSWER 保留未决问题；approval/删除不唤醒 waiting；空 review 正文不再阻塞修订完成。
- 同秒乱序编辑持久暂存并按前序回放。
- 移除评论中 `@bot` 直达修订的规则；普通文本和代码示例统一进入 Judge，仅授权的 Request Changes 绕过 Judge。
