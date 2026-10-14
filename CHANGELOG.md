# Changelog

- M5 F4：Docker smoke 保持 worker 运行并验证 serve 换 IP 后连接 epoch 更新，排除旧 online 状态及容器重启误报。

- M5 F2：补充真实并发接收提交顺序回归，并验证移除事务锁会导致测试失败。
- M5 F3：父观察器收到 SIGTERM 后回收观察进程组并释放运行状态锁。
- M5 F8：恢复测试从实际连接读取 fixture 端口，兼容 URL 省略端口的 libpq 配置。

## Unreleased

- Persistence 的业务 SQL 归 Flow/Delivery/Job/Worker 功能仓储，Store 显式持有各仓储实例；
  独立 Database 上下文提供共享连接、事务、时钟和共用锁，功能仓储只依赖该上下文。
  完整 schema 集中初始化；事件审计归 Webhook、drain 归 Worker，Projects/Webhook 使用统一入口。
  注入时钟覆盖创建和更新记录；Projects 局部更新字段有明确类型，事务退出后的功能对象拒绝继续使用。
  服务直接接收所需的具体仓储，业务调用使用 `self.flows`、`self.jobs` 等；Store 负责装配、初始化和关闭。
  跨仓储事务使用 `repository.using(tx)` 显式绑定同一连接，普通实例保留自己的连接范围。
  Webhook 重放、生命周期辅助、CLI/live smoke 和测试装配同步迁移。
  Runtime 统一初始化工具、effects、结果应用、路由与 conversation broker；服务删除仅用于初始化
  下游对象的仓储参数，Gateway 直接使用工具和结果应用。Webhook 使用事务绑定的路由/控制服务副本。
  跨表提交、锁顺序、数据库和持久消息合同保持现状。

- Control 内部直接依赖具体配置、仓储与服务，删除 12 个 Protocol 和 coordinator 的工具/结果转发；
  停止操作由直接注入的 StopStore 执行。API 显式接收 runtime，配置在实际入口构造；原始 socket 与接收泵连接
  分别表达传输和消息处理边界，派发授权、结果互斥、恢复及 ACK 规则保持现状。
  CLI 禁止 Uvicorn 覆盖 runtime 的日志配置，保留异常与查询参数脱敏和结构化 JSON 输出。
  控制服务端迁入 `app/controler`，按 `events/workers/workflow/review/reconciliation` 归组，
  根目录拥有 API、`ServerSettings` 和 `ServerRuntime`；内部导入及 Judge 进程入口同步迁移。

- GitHub 写入拒绝统一依据真实 HTTP 或未发送证据，GraphQL 部分 mutation 的合成状态码
  保留诊断用途，不据此清除写入不确定性。PR 创建回执唯一性规则由结果合同统一。
  启动恢复删除恒为零的计数；RevisionTask 直接拥有字段，CLI 控制端导入归对应命令。
  数据库、协议版本与任务 JSON/Schema 保持不变。
  逐项审查全部测试，删除无生产路径的异常与重复参数；补回 INITIAL 重试和 GraphQL 仓库改名覆盖，恢复夹具使用真实流程状态。
  常用和轻量依赖改为模块级导入；仅保留循环依赖、角色隔离及配置实例化入口，测试合并重复导入。

- 审查上下文的普通评论与 review 正文复用 `RemoteFeedback`，删除多来源 REST 讨论
  读取入口；每类单次读取 21 条并保留前 20 条，review 使用实际更新时间。空作者
  保留在批量中供上下文展示，持久反馈扫描继续跳过。文件和 inline 片段使用 SDK
  已有类型，明确处理文件可选字段的 Unset；完整线程仍使用 `ReviewThread`。

- GitHub 反馈读取统一 GraphQL：`fetch_threads`、`fetch_comments`、`fetch_reviews` 使用
  原生游标和可指定的批量大小，统一返回不可变 `Batch[T]`。thread 同请求携带首批评论及
  独立评论游标；持久扫描进度收归 control，删除旧 Page 类型和混合查询接口。
  评论事实改为嵌套 Batch，不读取旧评论列表或旧扫描状态；完整覆盖和 resolve 校验保留。

- GitHub 按 App、Installation、Repository、Issue、PullRequest、ReviewThread 和 Project/Item
  组织为不可变资源；查询返回新实例，已承载状态的更新返回确认字段改变的副本，使用标准序列化。
  控制层按持久身份绑定资源，分页上下文加工收归 control，删除 GitHub/Projects 协议和旧 client 入口；
  每种资源使用同一个模型承载事实与操作，删除平行的 Snapshot/Status 类型；列表和持久化恢复使用同类资源，
  私有父绑定及 SDK 认证不进入 JSON，worker 离线解析不加载 SDK。保留原 JSON 字段顺序、逐操作重试与响应丢失恢复。
  统一未知字段声明，PR 私有工厂直接生成资源；错误边界改为上下文管理器，删除操作回调与 lambda。
  原生客户端复制配置隔离写入重试，文件与讨论分页收归 PR 的异步迭代方法，review context 只保留审核加工；副作用复用已绑定资源，删除重复测试与替身查询转发。
  评论分页直接绑定 Issue/PR，review 反馈和线程单页请求由 PR 方法实现，删除独立转发函数与多余的纯模型延迟导入。
  资源、分页结果和错误类型集中由 `app.github` 导出；SDK 仅在实际构造时加载，包外导入统一使用公开入口。
  线程读取显式绑定持久 PR 身份，冻结上下文直接使用查询返回的资源，清除旧双模型的重复赋值。
  线程查询直接生成最终资源，根身份校验覆盖全部评论页并统一转换远端错误；去除集合回取和冗余可空分支，更新测试契约及命名。
  控制测试显式启用资源替身，删除全局自动安装、重复 App 操作接口及动态分派；原生 SDK 测试继续直接验证真实资源方法。
  评论、反馈页、ref、凭证、合并事件、Project 绑定配置与目标数据按概念归属移入 GitHub 包，统一导出错误和重试计时，删除旧 domain GitHub 模块及共用事实基类。

- Control 的批次 SQL 收回 ReviewStore；评论统一明确拒绝的判定，保留各自的事务授权。
  PR 身份核验及生命周期提交集中到 lifecycle，实时结果和后台恢复共用已有 ResultApplication
  及逐 job 互斥。保留现有数据库结构、消息协议和响应丢失后的只读恢复语义。

- RevisionTaskResult 直接拥有 batch_id 和 feedback_refs，删除未独立使用的 ReviewResult
  父类；字段约束、JSON wire、schema 和协议 v13 保持不变。

- Assignment 准备业务统一由 coordinator 完成，直接构造既有消息；签发后复查未启动状态、
  token/epoch、租约和 delivery。gateway 保留心跳、取消清理和发送前授权，读取重试与签发失败政策不变。

- CodingAgent start/resume 显式接收当前 Flow 目录，CLI 与进程目录保持一致；删除 context.cwd
  和未使用的 push_repository，VCS 只接收 push token。flow MCP 按当前 endpoint/token 配置，
  缺少 endpoint 时显式关闭，覆盖修订 resume 与 fallback。

- 编码审计以 jobs 的持久结果、digest、接收/应用时间及 revision invocations 为来源；停止
  写入 codex_runs，新库不再创建该表或索引。已有旧表和数据保留，外部 SQL 消费者需迁移；
  接收/应用时间不表示真实进程执行时间。

- 历史文件查找优先活动目录，找到即返回；移除重复文件报错、候选路径重复检查和 3 秒
  遍历计时，保留条目上限及打开文件时的访问校验。

- worker 历史查询只接受单个 session ID、cursor 和 limit，移除 SessionRef、HistoryCursor；
  control 负责 Flow/Attempt 归属和跨会话分页，每个 HTTP 页查询一个会话。旧游标返回 409，
  需重新加载；wire protocol 升至 v13，control/worker 须同步升级。

- worker 按 execution、transport、state 归组；同步模块引用与运维脚本，运行入口、消息协议和持久化格式不变。

- worker 进程内提供单任务 HTTP MCP，仅开放 open_pr；Codex 注入 URL 和临时 Bearer，移除 STDIO 子进程与 Unix socket 桥接，复用既有 PR 交付链。

- 历史查询消息分流、并发回复及连接结束清理归入 WorkerClient，删除 HistoryConnection；历史读取组件和 WebSocket 消息格式不变。

- WorkerSession 统一当前任务、持久化状态、结果归档/恢复与 Codex 历史查询；替换 WorkerJournal，保留既有 journal 格式和协议。

- 统一编码执行、结构化回复与任务结果的类型名称；保留首次/修订结果区分，序列化字段和执行行为不变。

- triage 作为 control 内的反馈批次前置判断，仅 REVISE 创建 worker job；删除独立 triage job、
  领取 token、本地任务恢复和结果契约。复用冻结批次、工作流事务与现有 coordinator；
  worker 不再执行 triage 或加载 bub/Judge。Judge 密钥迁到 serve；wire protocol 升至 v12，
  升级前排空旧 assignment、结果及 journal，再同步升级 serve/worker。共用安装依赖仍包含 bub。

- serve/worker 的配置类和进程级实例分别放入各自 config 模块，直接继承 BaseSettings，实例首次访问时创建；删除公共配置模块，启动入口继续显式传入 Runtime。

- 新增按 flow 查询 Codex 会话的 control API，通过现有 WebSocket 请求原 worker 按需读取历史；
  支持角色、工具调用关联、游标分页和独立 Bearer 认证，不新增会话队列或持久化。
  wire protocol 升至 v11，control/worker 需同步升级。持续接收器保证 control 准备任务、处理工具
  和应用结果期间仍可分流会话响应，避免已返回的历史被阻塞至查询超时。

- 按职责整理 domain/contracts/persistence/github/control/worker/telemetry 包，更新导入与部署入口；
  分离日志和指标，worker 启动不再间接加载数据库模块。CLI、数据库 schema 和网络协议保持不变。

- 精简内部结果应用与 CodeAgent 契约：仅按 job ID 应用持久结果，executor 负责严格解析
  原始 agent 输出；删除 AgentRun.output 与 SettleScheduler，统一插件异常隔离和重复配置字段。

- 修复公平领取时非候选 worker 替其他 worker 持锁造成的双空结果；候选无锁读取，
  仅锁请求方并复查 epoch、在线状态、空闲槽位及 drain/binding，增加确定性并发回归。

- WorkerGateway 的 stops 参数改为 execution_control，接口改为 WorkerExecutionControl，明确派发授权与停止控制职责。

- 未部署阶段统一到当前完整 schema、wire protocol v10 / journal v3；删除历史迁移、旧任务/effect
  与升级分支，交付结果字段改为 delivery_receipt，上下文共用当前 PR 模型。旧开发数据不自动转换；
  首次运行使用新数据库与 worker root。同版本恢复、重放、摘要和并发保护保留。

- 精简重复测试与 worker 替身，保留真实事务回滚覆盖；v9 回执兼容测试改用固定数据和摘要基准。

- review 标签更新与旧账本重放改为增量操作，保留用户标签；统一 PR 远端读取与交付回执类型，
  保持 v9 JSON、终态摘要和冻结上下文兼容。worker 显式要求持久 journal、身份及停止控制，
  tool caller 按执行传入；移除闲置 ReviewItem CRUD、webhook handler、sandbox/dispatch 接缝及重复 VCS 字段。

- 统一线程完整读取与持久分页的评论/回复 64 位 ID、线程解析和 GraphQL 游标校验；
  保留快照请求预算与分页片段的保守完整性，避免相同评论在不同读取路径产生字段漂移。

- 将反馈批次与租约恢复决策提取为纯规则；Store 保留并发 fencing 和原子写入，补充回执竞争与回滚测试。

- 将 INITIAL 交付校验提取为共享模块，解除 results/effects/lifecycle 循环依赖，并增加依赖边界回归检查。

- M5 F8：增加 systemd 示例、维护/冷备/轮换 runbook、10 条 Prometheus 告警及测试，
  提供隔离 PostgreSQL dump/restore 演练与完整性校验；复制 root 的活动恢复仍明确阻塞。

- M5 F7：增加零副本起步的 Kustomize base、独立 schema Job、RWOP worker PVC、内部 TLS
  和可选 Cloudflare overlay；新增有界 DB readiness 与 worker 本地探针，集群验收待环境。

- M5 F6：协议 v9 持久停止请求与 nonce 凭据、取消确认前保留 binding、丢失 ACK 重放、
  只读核对已发送 effects；持久 worker drain 允许原 Flow 收尾，补齐 SIGTERM 接线。

- M5 F5：增加字段白名单日志、独立 bearer 鉴权的内部指标和后台只读 PG 快照；采集故障
  保留旧值并暴露陈旧度，重放不重复累加持久执行统计。默认关闭 metrics。

- M5 F4：增加非 root serve/worker 镜像、TLS Compose 部署、可选 Cloudflare profile 与
  镜像构建/候选 digest 验证发布流程；本地无 Docker，实际构建及真实任务验收待 CI/环境。

- M5 F3：增加默认只读的真实闭环验收工具、显式测试 Issue 创建和结果未知保护，
  记录同一 Flow/PR 的修订 head 与归档事实；真实模型/GitHub 矩阵仍需运行环境。

- 修复 M5 Webhook 退避越过取消：同仓库按持久接收序号消费，阻塞项保留顺序。
- M5 F2：Webhook 在 202 前持久入库；业务转换与 inbox 完成同事务，支持重启重放、有限重试、
  blocked 诊断/重试、请求体限额及完成数据保留清理。旧 events 状态不明时保守阻塞。

- M4 收尾：PR 重开时持久退休已有反馈版本的强信号授权，超过单批上限、WAIT、空正文和重启
  不再绕过重新分诊；新反馈版本仍可正常授权修订。补齐 OPEN 轮询与限流退避跨重启回归。

- F9 返修：Projects 明确拒绝的建卡请求可恢复重试；已知归档结果等待通知时不再误标 Blocked。

- M4 F9：独立持久 Projects 状态投影，支持分页、响应丢失核对、版本竞争、退避及 CLI 诊断/重试，不阻塞归档释放。
  恢复验证覆盖已有归档卡片发现、人工恢复和跨重启 retry，复用原卡而不重复 add。

- M4 F8：可选组织 Projects v2 配置、只读目标校验、分页 item 读取及独立 add/Status API。
  阻塞返修：分页显式包含归档和非归档卡片，避免误判不存在后进入不确定建卡状态。

- 保留 CLOSED PR 的失联执行现场，重开前清理重新入队的旧任务，并保留查询期间到达的对账提示。

- F7 返修：永久准备失败也隔离旧领取者；耗尽保留根因；结果核验持久退避并遵守 Retry-After。

- M4 F7：尚未派发的只读 GitHub 准备支持持久有界重试，默认 5 次连续失败、15 分钟窗口；
  同事务保存退避与原 worker/token/epoch 条件重排，不消耗 revision_cycles。过期窗口不再读取，
  慢重试读取按剩余时间取消；成功后清预算。上下文/线程候选只在整轮成功后冻结，SDK 准备期读取
  不再叠加自动重试；CLI 显示准备诊断。签发凭据和模型执行保持既有失败语义。
  阻塞返修：执行中 flow.open_pr 的 ref 读取恢复 SDK 重试，持久准备/核验保留单次读取。


- M4 F6：未合并 PR 关闭或确认 head/base 漂移后暂挂原 delivery，保留 worker/repo binding；
  退休排队意图，旧结果/已发 effects 只收尾事实。生命周期 webhook 提前唤醒新鲜查询，
  重开须等待旧执行/结果收敛并核验原基线或冻结 worker 交付；反馈重新 triage，retry 不绕过暂挂。
  阻塞返修：CLOSED 缺少 journal 时保留原执行和结果入口，现场恢复后可重新打开 PR。

- M4 F5：持续扫描 PR Review 线程，包括已 resolved 线程内的新反馈；每次一个有界 GraphQL
  页，分别保存线程/评论 cursor，重启后续扫。页内容与进度原子提交，版本边界保护并发 webhook；
  人工状态只记录事实，不调用 reply/resolve/unresolve。缺失节点的扫描保持 incomplete。
  返修处理真实 SDK 的嵌套 NOT_FOUND，使缺失线程不再困住外层扫描。

- F4 阻塞返修：空正文不能挡住有效反馈；同正文的命令停用状态正确持久化。

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

- M5 F3 返修：没有 Issue 的观察返回未完成；统一进程预算覆盖数据库连接与外部命令。
- M5 F4 返修：修正 Codex 认证变量，网关动态解析 serve，smoke 边界断言立即失败。
- M5 F5 返修：reconcile 指标匹配调度资格，标准日志保留原级别与来源并继续脱敏。
- M5 F6 返修：准备取消无需不存在的 journal；历史 stop ACK 不受重绑影响；恢复取消中的 PR 身份与合并事实。
- M5 F7 返修：serve 的 DB/HTTP 不可用仅撤销就绪，不使用杀进程探针；worker 恢复执行时立即就绪。
- M5 F8 返修：恢复演练冻结 libpq 解析结果，Python 与 dump/restore 共用端口、密码文件与 TLS 设置。
