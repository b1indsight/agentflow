# 按 owner 和 Flow 保存个人阅读状态 — Feature record

**Stage:** Planning / 待批准；本次只有方案，没有产品代码、迁移或新测试。
**Issue:** [#88](https://github.com/b1indsight/agentflow/issues/88)
**Branch:** `flow/5`
**规划基线:** [main 的 bdfbdbf](https://github.com/b1indsight/agentflow/commit/bdfbdbfc6b2d1b11e8b76ee9da1b1b98599e9f06)

## 1. Initial plan

### Summary and motivation

用户在电脑读到评论的一段正文，换手机后应能继续；后台补录早期评论、编辑旧评论或改变讨论状态后，应能找到尚未确认的内容。看板刷新、抓取成功和滚到时间线末尾均不代表全部内容已经阅读。

这是 [web-console-kanban.md [025]–[027]](web-console-kanban.md#025--2026-10-17--确定按-flow-保存时间线及分层内容) 拆出的独立功能。保留 #85/#87 的归档、查询、业务状态和同步边界；原记录中的阅读建议未全部获批。本记录提出可批准的完整方案，不把候选规则描述为当前能力。

已核实的基础：

- [`ActivityStore._append/page`](../../app/persistence/activity.py) 在每个 Flow 的发布锁内分配并提交版本；页面按 `deleted DESC, updated_at DESC, version DESC` 选取快照内的对象版本，不能简单取最大版本作为当前对象。
- [`console_schema.py`](../../app/persistence/console_schema.py) 永久保存采集版本；本地业务审计 `source='flow'` 不进入当前会话。阅读功能不把这些内部审计重新塞进会话。
- [`OwnerAccess.authenticate`](../../app/controler/access.py) 验签并校验配置的 owner，但现在返回 `None`；[`_require_owner`](../../app/controler/api.py) 保护整个控制台路由。持久化个人状态需要在这条已验证的身份链上取得 owner 分区键。
- [`windowing.tsx`](../../app/controler/web/windowing.tsx) 的书签、折叠和正文段位置只在内存中；[`useConversation.ts`](../../app/controler/web/useConversation.ts) 的快照/缓存不构成持久已读证据。操作和工具分组的显示行可能包含多个原始条目。
- [`ActivityBody`](../../app/controler/web/activity.tsx) 与服务端正文接口已经使用展示正文的 Unicode 码点偏移；本地文本切片仍需处理 UTF-16 边界，不能直接把 JS 字符串长度持久化为服务端偏移。

### Goals, scope, and success criteria

- 按已验证 owner + `flow_id` 隔离已读记录与续读位置；同一 owner 的设备合并阅读证据，其他 owner 不继承。
- 新活动、可见编辑、讨论状态变化、删除标记和早期补录在选定规则下显示未读；重复同步及不可见元数据变更不重复提醒。
- 保存原始条目和正文段位置；跨设备恢复不要求重新扫描并下载此前全部正文。
- 提供实际可见确认、显式标记、跨设备位置冲突与失效锚点的可恢复交互。
- 只写个人 UI 元信息；不修改 Flow 状态、看板阶段、同步进度，不调用 GitHub/Worker 写入口。
- 服务端并发回归、浏览器真实可见性与两设备场景全部通过，且已有查询、折叠、分页和焦点行为不退化，才算实现完成。

非目标：认知层面的“确实读懂”、邮件/推送通知、新用户体系、多人协作权限、每设备长期位置历史、离线全文缓存、按轮次重置阅读状态、对旧归档自动过期。#85/#87 不依赖本功能上线。

### Proposed behavior

以下均为**待批准的推荐规则**。

#### 1. 对象身份、版本与未读语义

阅读对象是 `(flow_id, activity.key)`；原始 `key` 已包含来源身份，保留实际 `round_no` 和 `parent_key` 关系。Issue 号、发生时间、分组首条、DOM 下标、页号都不能代替它。

采用“语义阅读修订”：只沿着对象**实际被选中显示的版本序列**比较可见内容。相邻可见投影相同则沿用修订，不同则用本次归档版本作为新的修订标识。修订是精确身份，不能解释成“此编号之前都读过”。

- 投影包括来源/类型、作者、标题、规范展示正文、删除标记、父级、来源链接、执行/会话归属，以及 UI 展示的状态字段（如线程 `resolved`、检查状态/结论、操作名称）。按类型显式列白名单，并与展示测试对照。
- 排除观察/抓取时间、游标、同步健康、传输 receipt、仅内部使用的元数据。不能笼统忽略全部 metadata；补齐实际显示的工作流名或检查结论属于可见变化。
- 对象 A→B→A 产生三个不同修订；只读过最早 A 不能清除新 A 的未读。内容哈希只比较相邻投影，不能作为跨全部历史的已读键。
- 未成为当前显示版本的迟到旧编辑仍可归档，但不会让当前条目重新未读。只有实际观察到的版本可参与判断，不能重建采集前未观察的变化。
- 当前状态只提示每个原始对象一次，不为已经被新版本取代的每个中间版本叠加通知；历史版本仍可查阅。

| 场景 | 预期个人阅读状态 |
| --- | --- |
| 新 key，或发生时间更早的新补录 key | 未读；补录保留真实发生时间和“新发现的早期记录”说明 |
| 已读评论正文更新 | 新修订未读；旧正文的覆盖区间不继承 |
| 讨论 resolved → open → resolved | 每次可见转换产生新修订，不按相同正文哈希去重 |
| 同版本重试、仅观察时间/传输标记变化 | 沿用阅读修订，不重复提醒 |
| 不赢得显示选择的旧版本晚到 | 当前未读状态不变 |
| 远端删除已确认，追加 tombstone | 删除说明作为新修订未读；旧归档正文仍保留 |
| 读过新版后收到旧版回执 | 只合并对应修订，不改变新版回执 |

#### 2. “可见”与显式确认

首版建议只有两种完成已读的途径：完整可见证据，或用户主动标记。UI 可显示“未读”“部分已读”“已读”；部分已读仍计入 Flow 未读数，避免把预览当作全文。

- 看板、预取、来源同步、成功 GET、显式刷新、定位、跳到末尾、overscan 挂载及浏览器恢复滚动均不提交已读。
- 页面须处于可见且聚焦状态，详情会话在前台，用户已经在该会话进行滚动、键盘导航或点击阅读操作，布局已稳定，才开始自动采集。后台、导航遮罩、收起正文、加载占位和恢复中的列表暂停采集。
- 建议同一可见区域连续停留 1 秒后才形成证据；短时间扫过不计。定时器使用单调时钟，失焦/隐藏/卸载立即取消；阈值需要用户确认。
- 无正文事件须实际看到该事件说明；有正文条目须看到标题/状态说明，且已确认正文区间并集覆盖 `[0, body_length)`。空正文不会因为空区间自动成为已读。
- 正文一段高于视口时，仅观察整段的相交、首尾哨兵或滚动到段尾均不足。前端在当前布局内累计实际可见且满足停留条件的纵向覆盖；完整覆盖该段后才上报它的码点区间。重新排版、换段、折叠或宽度改变清空未完成的局部几何证据。跨设备合并已经完成的段，不声称能合并两台设备各读一半的同一段。
- 表格或代码存在横向滚动时，还须覆盖其横向内容；未能可靠测量的 Markdown/内嵌滚动区域不自动确认所在段，提供显式标记。不能仅凭纵向经过就认定看过屏幕外的列。
- 折叠工具/操作组的摘要不代表成员已读。展开后对原始成员分别确认；线程头的已读不确认隐藏或未加载的回复。线程与分组标记汇总成员的未读状态。
- “标记此条已读”明确确认当前显示修订的完整展示正文；“将已加载条目标为已读”确认点击时捕获的精确集合，按最多 50 条分批。后续加载或同步到达的条目不进入这次集合；失败显示仍未提交的批次。首版不提供含糊的“读到此处”或隐式全历史已读。
- 提供“将此 Flow 标为未读”：重置个人证据，保留续读位置。首版不增加逐条手动撤销已读；如需要，在批准时扩充对象级 reset 设计。
- 已有归档首次启用时没有个人回执，显示未读；不伪造历史阅读。UI 提示“尚未记录阅读状态”，可显式确认已加载条目。

#### 3. 跨设备合并和位置冲突

已读是同一个重置代次、同一个语义修订下的集合并集：标题已见取 OR，完整显式确认取 OR，正文半开区间合并。重复请求幂等，延迟/乱序不会撤销其他设备的进度；绝不使用客户端时间或最大已读 ID。

每个 owner + Flow 有 `read_epoch`。标为未读使用当前 epoch 做比较并递增；旧 epoch 的回执返回 409。客户端丢弃旧证据，不能换成新 epoch 自动补发；须有新的主动阅读行为再采集，避免停在原处的后台/前台标签马上撤销手动未读。

续读位置独立使用服务端 `position_revision` 做 compare-and-swap（CAS）：

1. 进入详情只读取位置并尝试恢复；恢复过程不写位置、不标已读。
2. 只有前台聚焦的主动阅读产生位置写入。每个 tab 最多一个在途位置请求；等待期间只保留最新主动位置，成功后用返回 revision 提交下一次。
3. 服务端仅在 `expected_revision` 等于当前 revision 时接受，并递增。两个设备从同一 revision 写入只接受一个；不按更靠后的位置或客户端时间选择赢家。
4. 冲突时保留当前页面视口，提示“另一设备更新了续读位置”，提供“前往已保存位置”和“以本机位置继续”。第二个动作重新读取后进行新的 CAS；禁止后台轮询、失焦、卸载 beacon 或冲突自动重试抢回位置。
5. 请求带随机 `mutation_id`；响应丢失后 GET 核对最后 mutation，不能假定未保存。原请求可在相同 expected revision 下重试；已经有更新则冲突，不把它重新包装为新写入。
6. 旧响应按 Flow、挂载代次、epoch/revision 隔离；切换 Flow 后不得污染新页面。设备离线时只保留当前内存中的待提交证据并明确显示“未同步”；不建立无界离线队列。

#### 4. 续读和失效回退

保存原始活动 `key/version`、快照、父级、正文段的码点起点和原排序 `(occurred_at,key)`。不把分组显示键、HTML ID、像素 scrollTop 或下载游标作为跨设备身份。可继续保留本地像素书签以改善同设备临时恢复。

- 恢复到保存快照和正文段；分组成员映射回其分组，按需展开目标组/线程和读取目标正文，不确认其他成员。新设备可直接定位，无需从首页顺序拉完历史。
- 新快照中同 key 的版本改变：默认提示有新版本，允许继续原归档版本或前往新版本段首；不把旧偏移静默套到不同正文。
- 远端删除只追加标记，原快照仍可恢复；查看最新时显示删除说明和“查看归档版本”。
- 原版本被明确的离线维护清理或展示正文规则不兼容：返回具体原因，显示回退提示；选择同 key 的有效版本段首，其次同父级排序后的最近条目、之前最近条目、父线程头、Flow 时间线起点。没有内容则显示空态。
- 回退不会覆盖原保存位置，也不会确认内容；用户主动继续阅读后才以新 CAS 保存。库恢复导致快照超出当前 head 时也走可见回退，不能假装精确恢复成功。

### Technical design and affected modules

#### 数据类型与持久化

以下是拟定的最小核心模型。数字均为非负严格整数（版本和修订 > 0）；区间采用 `[start,end)`，按展示正文 Unicode 码点计。`owner_key` 只在服务端产生，不接受 URL/body 中指定 owner。

| 类型/表 | 字段、类型与含义 |
| --- | --- |
| `OwnerIdentity` | `key: str`：对经验证的 issuer + subject 做无歧义长度前缀编码后 SHA-256；不以客户端 email、设备 ID 或单 owner 常量代替。不同设备同身份相同，换 owner 或 issuer 不自动继承。 |
| `ReadingRevision` / `console_reading_versions` | `flow_id: bigint`、`version: bigint` 为归档版本关联；`key: text` 为原始身份；`reading_revision: bigint` 为该连续语义段首个有效归档版本；`format_version: int` 为展示/比较规则版本；`visible_digest: text` 为规范可见投影哈希；`body_length: bigint` 为展示正文长度。只记录曾赢得显示选择的版本，索引 `(flow_id,key,version DESC)`。 |
| `ReadingIndex` / `console_reading_index` | `flow_id: bigint` 主键；`built_through: bigint` 为已处理的发布版本；`format_version: int` 为构建规则。这个检查点只表示索引覆盖，不表示任何人读过。 |
| `ReadingAnchor` | `snapshot: int`、`key: str`、`version: int`、`parent_key: str \| null`、`occurred_at: datetime`、`body_offset: int`、`format_version: int`；锚定原始条目、所属线程和正文段。保存时由服务端核实身份/归属/排序，不能信任客户端伪造时间。 |
| `OwnerFlowReading` / `console_owner_reading` | 主键 `owner_key: text, flow_id: bigint`；`read_epoch: bigint=0`；`position_revision: bigint=0`；`anchor: ReadingAnchor \| null`（受验证 JSON）；`last_position_mutation: uuid \| null`；`updated_at: timestamptz` 仅用于说明保存时间，不参与冲突裁决。 |
| `ReadingReceipt` / `console_reading_receipts` | 主键 `owner_key,flow_id,read_epoch,key,format_version,reading_revision`；`header_seen: boolean`、`explicit: boolean`、`body_ranges: int8multirange`。区间并集可交换；只匹配精确修订。关联修订档案，不保存正文副本。 |
| `ReadingObservation` | `key: str`、`version: int`、`header_seen: bool`、`ranges: list[tuple[int,int]]`；客户端报告精确归档版本，服务端映射修订。每请求 ≤ 50 条、每条 ≤ 32 区间；拒绝负值、非整数、越界、跨 Flow 或无法映射的版本。 |
| `ReadingSummary` | `snapshot: int`、`head: int`、`read_epoch: int`、`unread_count: int \| null`、`partial_count: int \| null`、`index_ready: bool`；统计指定快照选中原始对象及回复，不含内部 Flow 审计；索引未就绪时计数为 null，不能用 0 冒充已读。 |
| `ReadingPosition` | `position_revision: int`、`anchor: ReadingAnchor \| null`、`last_mutation_id: uuid \| null`；读取不创建记录，无记录视为 revision/epoch 0。 |
| `ReadingWindow` | `snapshot: int`、`items: list[ActivityItem]`、`before_cursor: str \| null`、`after_cursor: str \| null`；前后续读使用绑定 Flow、snapshot、parent 与方向的独立不透明游标，不能伪装为首页前缀。 |
| `ReadingLocation` | `snapshot: int`、`requested: ReadingAnchor \| null`、`resolved: ReadingAnchor \| null`、`reason: exact \| version_changed \| content_removed \| format_changed \| snapshot_unavailable \| empty`、`window: ReadingWindow`、`parent_window: ReadingWindow \| null`；返回目标附近的有界窗口及所属线程定位，不下载全部正文。 |

完整区间可压缩成 `[0,length)`。持久碎片上限建议 256 段/修订，超过则保留已接受区间并返回明确的 `coverage_limit`，不把间隙填成已读；UI 允许继续形成更长连续覆盖或显式标记。发布后再以真实使用数据评估上限，不承诺总存储恒定。

#### 拟定 HTTP 与方法合同

全部路径位于 `/api/flows/{flow_id}`，由现有 owner dependency 保护，返回 `no-store`；写入复用同源 JSON 检查。Access 配置缺失 503、缺失/无效断言 401、非 owner 403，其他语义错误使用现有 `ConsoleFailure`。

| HTTP | 请求 / 结果 |
| --- | --- |
| `GET /reading` | 返回 `ReadingSummary` 与 `ReadingPosition`；可指定已打开的 snapshot，另返回当前 head，避免混淆当前未读与旧快照。 |
| `POST /reading/items` | 只查询最多 50 个精确 `key/version`，返回对应 epoch/修订、header/正文覆盖和显式确认状态；POST 是有界查询，不写回执。 |
| `POST /reading/observations` | `{read_epoch, items: ReadingObservation[]}`；合并已读证据，返回当前 epoch 和确认结果。 |
| `POST /reading/mark-read` | `{read_epoch, items: [{key,version}]}`，最多 50 个精确成员；显式设置完整确认。 |
| `POST /reading/mark-unread` | `{expected_epoch}`；原子递增 epoch 并清空旧个人回执，保留 position。旧 epoch 重试返回 409，GET 可核对结果。 |
| `PUT /reading/position` | `{expected_revision, mutation_id, anchor}`；CAS 保存并返回 `ReadingPosition`；409 携带当前 revision/位置，不触发自动接管。 |
| `GET /reading/locate` | 按保存锚点或显式 `{snapshot,key,version}` 返回 `ReadingLocation`；前后各最多 20 条，同父级查询。只定位，不确认。 |
| `GET /reading/window` | 使用 locate 返回的 before/after 游标，每次最多 20 条；返回 `ReadingWindow`，不改变既有 `/activity` 的前向分页合同。 |

看板列表复用当前分页查询，批量补充每个 Flow 的 `ReadingSummary`，不逐卡访问仓储；列表加载不写阅读状态。详情的条目查询按当前页面批量获取回执；所有读取都是本地归档查询，不访问 GitHub/Worker。

拟定 Python 公共方法（DTO 定义在 `app/contracts/console.py`，复杂实现归新 `app/persistence/reading.py`）：

- `OwnerAccess.authenticate(token: str | None) -> OwnerIdentity`：保留验签/授权，返回已验证的分区身份；`_require_owner` 放入类型明确的请求上下文。
- `ReadingStore.index_version(flow_id: int, version: int) -> None`：同发布事务内处理有效显示转换，源版本与修订原子可见。
- `ReadingStore.rebuild_batch(flow_id: int, limit: int = 500) -> bool`：按已提交版本重放一个有界批次，返回是否追平；不写回执。
- `ReadingStore.summary(owner: OwnerIdentity, flow_id: int, snapshot: int | None) -> ReadingSummary`；`summaries(owner: OwnerIdentity, flow_ids: list[int]) -> dict[int, ReadingSummary]`：单 Flow 或有界列表聚合。
- `ReadingStore.items(owner: OwnerIdentity, flow_id: int, items: list[ActivityReference]) -> ReadingItems`：`ActivityReference` 仅含 `key: str, version: int`；`ReadingItems` 含 `read_epoch: int` 和逐项 `ReadingItemState`，后者含 reference、修订、header_seen、explicit、ranges 及 `unread|partial|read`。
- `ReadingStore.observe(owner: OwnerIdentity, flow_id: int, epoch: int, items: list[ReadingObservation]) -> ReadingItems`：原子并集；`mark_read(owner: OwnerIdentity, flow_id: int, epoch: int, items: list[ActivityReference]) -> ReadingItems`：同样校验后显式确认。
- `ReadingStore.mark_unread(owner: OwnerIdentity, flow_id: int, expected_epoch: int) -> int`：递增并返回 epoch。
- `ReadingStore.position(owner: OwnerIdentity, flow_id: int) -> ReadingPosition`；`save_position(owner: OwnerIdentity, flow_id: int, expected_revision: int, mutation_id: UUID, anchor: ReadingAnchor) -> ReadingPosition`：读取与 CAS 写入。
- `ReadingStore.locate(flow_id: int, anchor: ReadingAnchor) -> ReadingLocation`：精确定位/有说明的回退。HTTP 层始终先认证，且自行从 owner 状态取得默认锚点。
- `ReadingStore.window(flow_id: int, cursor: str, limit: int = 20) -> ReadingWindow`：校验游标归属与方向后有界续读；不产生回执。

响应 DTO 中不暴露其他 owner 的状态。新路由继续走 `runtime.console.db` 的线程边界，不阻塞 ASGI 事件循环。

#### 发布、事务与索引

不能修改归档正文来记录阅读状态。独立修订侧表复用已有选中版本规则，避免另写一套不同的“最新活动”判断。

1. `_append` 保持每 Flow 发布锁，新增归档后比较前后选中版本；只给赢得选择的非 Flow 审计版本建立映射。重复投递不生成修订。索引更新与归档事务一起提交或回滚。
2. 已读写入先按固定顺序取得 Flow 共享锁，再取得 owner + Flow 状态行锁；发布/回填只取得 Flow 发布锁、不反向取得 owner 锁。首次状态行用唯一键 upsert 后锁定，不能依赖不存在行上的 `FOR UPDATE`。批次限制控制锁持有时间。
3. 所有 observation/mark-read 在锁内核对 epoch、版本归属、展示长度和规则版本后并集合并；整批失败不留下部分确认。手动未读与这些写入共享 owner 状态锁，延迟回执不能穿过重置代次。
4. 位置写入在同一 owner 状态锁内核对 CAS；不递增活动快照或业务 state_version，不排队同步，不触发工作流。
5. 所有 source='flow' 的发布不产生阅读对象。索引就绪检查可以越过纯内部审计版本，不能把这些编号空隙解释为缺失或未读。

既有档案按真实发布顺序分批重放，模拟每个时点的显示选择；不能只给当前行赋新修订，否则旧快照无法关联回执。使用单独迁移命令执行有界批次并记录 checkpoint，重启可续跑；批次在 Flow 发布锁内提交。未追平的 Flow 显示“阅读状态初始化中”，其阅读写接口返回 503，不显示假零未读，不阻塞既有会话读取/同步。追平后新发布事务维护增量；初始化期间新行继续进入待重放范围，新 Flow 没有历史时直接建立索引。

未读查询使用与时间线一致的当前对象集合，JOIN 精确修订回执；大 Flow 的聚合成本需通过 EXPLAIN/代表性档案测量。先复用已有对象索引，不预先新增第二张 current-head 表；如不能满足看板分页预算，在实现记录中列测量结果再决定物化聚合。API 响应有界不代表扫描成本或历史存储有界。

#### 前端责任

- `api.ts` / `types.ts`：类型化同源请求、409/503 分支、取消与返回代次校验；不发送 owner 字段。
- 新 `useReading.ts`：owner/Flow 阅读状态、精确回执批次、epoch 与位置 CAS；`useConversation` 继续只负责归档快照和有界缓存。
- `useConversation.ts`：从仅首页前缀的页缓存扩展为有界的锚点窗口，显式保存前后缺口与游标。加载目标附近窗口不把之前未加载的索引范围标为已加载；跨边界分组注明内容尚未加载，不能为完整分组主动拉完历史。
- `activity.tsx`：原始条目及线程的状态/显式按钮、正文段可见证据；分组汇总不代替成员身份。
- `windowing.tsx`：在布局可靠后提供实际可见范围和恢复完成信号；保持当前 60 行、240 条活动正文、4 个 Flow 缓存预算，不把 overscan 报告为阅读。
- `App.tsx`：看板/侧栏未读提示、续读提示、冲突/失效锚点操作；刷新保留当前位置，不替用户跳去其他设备的新锚点。
- 现有 `ReadingState` 的折叠、焦点和本地像素状态继续保留。个人持久阅读只同步必要的语义锚点和证据，不把临时 UI Map 整体上传。

### Alternatives and rationale

- **最大时间、最大活动 ID 或快照即已读：** 简单但会漏掉早期补录、旧条目更新，且无法表达跳过的页面/正文段；拒绝。
- **仅精确归档版本回执：** 能防跨版本误确认，但会把仅传输/观察信息变化重新报未读；阅读修订消除这类重复。实际展示改变仍需提醒。
- **全历史内容哈希已读：** 会吞掉 A→B→A；只比较相邻有效投影，保留转换身份。
- **仅本地存储/像素位置：** 无法跨设备恢复，布局与分组变化后漂移；仅保留为同设备临时优化。
- **时间戳后写覆盖、最大阅读位置、自动 CAS 重试：** 客户端时钟偏差和后台延迟请求可覆盖主动位置，也不允许用户返回早处阅读；采用冲突可见的 CAS。
- **设备租约、心跳和位置 CRDT：** 首版不需要自动争抢写者；单位置 CAS 与明确接管更少状态。不引入额外服务或依赖。

### Costs, risks, and mitigations

- 语义投影是新增维护成本：正文规范化须复用 `_display_body`，类型白名单与真实展示对照；字段增加时检查是否改变阅读意义，不用全 metadata 哈希掩盖契约。
- 内容规则升级可能改变段位置。`format_version` 隔离旧偏移/修订；重建完成前禁止混用。不能证明展示相同则不继承旧覆盖，提示规则已变化；不能宣称重建总能无损保留已读。
- 自动可见性只是产品约定，不能证明理解。保守采集可能使部分条目保持未读，提供显式确认；不能为消除未读徽章而推断隐藏内容已读。
- 回执集合随阅读对象/修订增长，永久档案本身也增长。只存布尔与合并区间；显式重置可清掉旧代次回执。避免后台请求日志或逐次滚动事件表。
- 换 owner/issuer 后不自动迁移个人状态；如要转移身份，由后续明确管理操作完成。Access 应用变化是否保持 subject 必须在目标环境确认，不能承诺跨不同身份提供商自然合并。
- 当前档案按既定规则永久保留，远端删除仍为 tombstone。本功能不增加 TTL/清理任务。未来明确硬删除归档时，同事务清理对应修订与回执；锚点保留最小定位元信息并报告失效，Flow 真正删除时级联清理个人记录。不得自动删除受保护的 Flow 审计。
- 回滚应用可停止使用新增表，保留归档与个人数据；恢复新版本后先追平索引。索引未完成不影响旧 API 和同步；发布钩子的真实代码/数据库故障仍须回滚并报告，不能吞掉异常后假装归档和修订一起成功。这是新增发布路径的风险，需用故障注入验证恢复。

### Implementation order

1. 批准下列产品规则；实现前核对发布路径、显示字段和身份返回值的全部调用方。
2. 定义 DTO、身份分区和阅读修订侧表/分批回填；验证增量与重放结果相同，不修改永久归档。
3. 实现个人回执、epoch、位置 CAS 和 owner/同源校验；覆盖事务竞争后再接 UI。
4. 实现有界定位、历史/失效回退；接前端只读状态与显式标记。
5. 接可靠可见性、正文覆盖与跨设备位置；运行真实服务端加两浏览器的集成验证。
6. 更新部署/接口说明，执行完整回归、视觉检查与机制消融，再走仓库代码审查流程。本方案本身不授权上线或部署。

迁移先建表并分批回填，再启用该 Flow 的阅读操作。不得依据 Flow 执行状态或 GitHub 同步状态自动生成个人已读基线；历史默认未读是需要批准的产品取舍。

### Validation and ablation strategy

以下是实现后要执行的验收，不是本次已验证的功能：

| 层级/场景 | 必须观察到的结果 |
| --- | --- |
| PostgreSQL：新建、编辑、A→B→A、无可见变化、tombstone、早期补录、迟到旧编辑 | 当前对象未读与语义修订一致；重试不重复提醒；同一 key 不因历史版本多而重复计数 |
| 两连接控制提交次序、回滚和并发发布/回执 | 快照没有未提交空隙；只确认请求里的精确对象；不可拿较大 ID 覆盖未读成员 |
| 两设备读取不同段，重复/乱序/响应丢失 | 同 epoch 同修订并集合并；其他修订不混合；重试幂等 |
| mark-unread 与旧回执交错 | epoch 冲突后仍为未读；旧页面不能自动换 epoch 重放 |
| 位置 CAS：两个读者、旧 tab、离线队列、GET 后延迟 PUT | 仅一个同 revision 写入成功，冲突可见；自动恢复/后台请求不写位置 |
| 未认证、错 owner、伪造 owner、跨 Flow/版本、跨源、非 JSON、无效区间 | 在业务 I/O 前拒绝身份；状态和个人数据隔离；没有 GitHub/Worker 写调用或工作流变更 |
| 看板/详情/同步/刷新/预取/跳过未加载页面 | 请求成功本身不写回执；落点之外和被跳过的历史仍未读 |
| 浏览器：实际阅读短文、长段部分可见、跨正文页、emoji、Markdown、代码换行、横向表格 | 未覆盖部分仍未读；横向未展示内容不因纵向覆盖误确认；码点与服务端长度一致 |
| 折叠组/未展开线程、虚拟窗口重挂载、分组首成员更换、60 行边界 | 保持原始身份；隐藏成员不被摘要确认；焦点/预算/偏移无退化 |
| 窗口隐藏/失焦、移动端遮罩、布局改变、程序化恢复 | 取消未完成停留证据；无后台回执/位置覆盖；新用户操作才恢复采集 |
| 两个浏览器 context 连真实受保护 API | owner 状态互通、不同 owner 隔离；真实数据库 CAS 和并集合并正确，不能仅用 route mock 声称跨设备通过 |
| 有界定位、目标删改、版本/规则变化、缓存淘汰、数据库恢复 | 有准确原因和可操作回退，未自动覆盖原锚点；无需拉完全部历史 |
| 旧档案回填与在线增量、重启续建、故障回滚 | 相同修订映射；未就绪有明确状态；既有后端继续工作 |

消融在临时副本中每次只移除一个机制：用最大时间/ID 代替精确回执、用全历史哈希去重、忽略正文未覆盖区间、去掉 epoch、去掉 CAS、把 overscan 当可见、把 UTF-16 当码点、移除 owner 分区。相应反例必须失败，恢复后通过；不保留实验开关。实施完成后、自动代码审查前执行。

常规检查沿用 CI：`uv run ruff check app tests`、`uv run mypy app tests`、UTF-8 PostgreSQL 下 `uv run pytest`、前端 `npm run format:check` / `npm test` / `npm run build` / `npm run test:e2e`、`python scripts/build_console.py` 和 `uv build --no-sources`。新增 UI 后检查桌面/手机默认、部分已读、冲突和锚点回退截图；真实 Access 登录与实际双设备另做部署验收。

### Open questions and deferred work

**批准时需要决定：**

1. 是否接受“前台主动阅读 + 1 秒实际可见 + 全段覆盖”作为自动已读；部分阅读仍未读，折叠摘要不确认成员？推荐接受；不能证明用户认知理解。
2. 是否接受精确集合的显式已读和 Flow 级标未读，首版不做隐式全历史已读及逐条撤销？推荐接受。
3. 是否接受旧档案首次全部未读，工具/操作/回复和删除标记计入，内部 Flow 审计除外？推荐接受，界面解释初始化基线。
4. 是否接受跨设备 CAS 冲突后由用户选择接管、默认恢复旧快照段位置并提示有更新？推荐接受，不自动合并两个阅读位置。
5. 是否接受更换 owner/issuer 不继承状态，历史版本永久保留、无自动清理？推荐接受，与既定归档边界一致。

**实现中核实：** 可见字段白名单、Markdown/横向溢出区域的可见覆盖实现、回填与未读聚合的查询计划/预算、恢复窗口与现有前缀分页缓存的整合、Access subject 在目标配置下的稳定性。若可靠可见性必须引入新渲染拆分或改变正文展示合同，先补方案变更，不能以“DOM 已挂载”降级验收。

**后置：** 推送提醒、每设备书签历史、持久离线队列、多 owner 管理/迁移、全部归档一键已读、逐条标未读。它们不作为本期默认能力。

## 2. PR change timeline

<!-- 初始提案尚未获批。后续仅追加实质需求、设计及实现变化，保留本初始方案。 -->
