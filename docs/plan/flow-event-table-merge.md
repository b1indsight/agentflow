# 三项合表：Issue 归属、当前交付与 Webhook 收件

**状态：已实施，未部署。** [PR #185](https://github.com/b1indsight/agentflow/pull/185)。按后续要求，存量库改为手工升级。下文的启动自动迁移是初版方案，已被取代。

## 1. 初始计划

把 `issue_flow_owners` 和 `flow_deliveries` 并入 `flows`，把 `webhook_inbox` 并入 `events`。目标是少维护三张表，同时保留现有 Store 接口、控制台展示、标签、Projects 和 Webhook 行为。`attempts`、`jobs`、`flow_tool_calls`、MCP 和 Worker 协议均不在本 PR 范围。

### Flow 保存 Issue 归属与当前交付

- `flows.is_issue_owner BOOLEAN NOT NULL DEFAULT FALSE` 表示最后一次创建或恢复时选中的展示归属。仅 Issue Flow 可为真，对 `(repo, issue_number) WHERE is_issue_owner` 建唯一索引。创建或恢复时，沿用 Issue 锁，在同一事务先清旧标记再设置新标记；归档不清。按旧映射精确回填，检查目标 Flow 的仓库与 Issue 一致，不用最大 ID 推断。原 `version` 不参与应用读取。改用标记实现 `FlowStore.select_owner/is_issue_owner`、Project seed/refresh 和归档标签检查。
- 用 `delivery_` 前缀将旧交付表除 `flow_id` 外的字段以原类型放到 Flow 上，保留独立版本与创建/更新时间。无交付时整组为空；有交付时至少分支、状态、版本和时间非空。因此无分支、有分支但尚无 PR、已开 PR 是三种不同情况。保留 `DeliveryStore` 的 `FlowDelivery` 返回值，SQL 投影使用原字段名；只更新交付列，不以交付标题、状态或时间覆盖 Flow 同名字段。恢复后仍使用同一 Flow ID，重置当前 PR 并推进交付版本，历史 PR 仍从归档活动查询。
- 更新 PR/分支查询、索引、分配、评审、对账、指标、控制台同步及 `console_facts` 投影，去掉到旧表的 JOIN。保留交付导致的真实状态活动；归属切换或技术回填不得伪造生命周期、活动或重复归档证据。核对 Flow 账本比较列和重放，不把纯协调列纳入状态转换。

### 一次 Webhook 投递只占一行

`events.delivery_id` 仍为主键。共用事件类型与载荷，业务侧保留可空 `flow_id`，用可空 `recorded_at` 表示已登记（迁自旧 `events.processed_at`）。收件侧沿用 `webhook_inbox` 的顺序、原始字节摘要、接收时间、状态、重试、领取凭据、完成时间和错误码。没有收件信息的业务凭据不进队列、不出现在收件列表与队列指标中。`done` 与 `recorded_at` 独立：被忽略的投递也可以完成。

| 旧记录 | 合并后的语义 |
|---|---|
| 只有 inbox | 按原状态继续处理或保留已完成收件；不补业务登记 |
| 两边都有 | 保留原队列顺序、领取/重试数据和业务登记；队列载荷已清理时不从审计副本恢复正文 |
| 只有 events | 保留业务凭据；不伪造接收时间、摘要或待处理任务 |

保留 `WebhookStore` 的接收、领取、登记、清理契约。收到时只创建或填充收件信息，已有收件仍检查类型和摘要。`record_event` 仅首次设置登记时间才返回成功；允许没有收件行时直接登记。在消费者原事务内提交登记、业务变更及收件完成。继续按仓库串行领取、校验租约与 token；blocked 的队首仍阻挡同仓库后续投递，其他仓库可继续。

按**收件完成时间**分批清理：7 天后清正文；90 天后无业务登记的已完成行可删除，有登记的仅移除收件信息。此后同 ID 重投可取得新的队列位置，但不得再次执行业务。不要用 `state='done'` 代替业务登记，或用统一 TTL 删除全部凭据。回填后下一收件序号须高于存量；无收件的行不能挡队首。

### 迁移、风险与验证

1. 在 `schema.py`、`console_schema.py` 与现有初始化事务中支持空库、当前旧库升级及重复启动。先加列、按旧行回填并验证，再切换视图、索引和触发器依赖，最后显式移除三张旧表。不用 `DROP ... CASCADE` 隐式移除依赖，遇到不一致则回滚。保留旧版 `scripts/migrate_flow_states.py` 到当前初始化的升级路径，区分历史 SQL 与仍在线执行的 SQL。
2. 分别切换 Flow 与 Webhook 的读写方；保留公开接口，不做双写和长期兼容视图。折衷是 `flows` 行变宽，交付与 Flow 更新竞争同一行；复用现有锁顺序，按所属列更新。合表优于保留旧表的前提是语义等价，不以减少查询次数作为未测量的收益。
3. 在一次性 PostgreSQL 中用升级前快照作对照，覆盖旧 Flow 恢复再归档仍为 owner、直接任务无 owner、无交付与 `none`、跨轮新分支、旧 PR 查回原轮次。对照升级前后字段、版本、时间、控制台投影、账本及归档证据；二次启动不重复回填。覆盖 Webhook 三类旧记录、接收后事务失败再试、并发队首领取、过期 token、重复投递与内容冲突、跨 7/90 天清理后重投。运行现有 Projects、Flow 归档、Webhook、迁移测试、全套 pytest、ruff 与 mypy。去掉归属标记（只取最大 ID）和混用完成/登记的负例均必须能被上述用例区分。
4. 不支持旧控制端与新 schema 混跑。正式升级前需停写、备份并验证可恢复性；若初始化事务失败则保持旧库，提交后回退须协调停写、恢复快照及旧程序并核对期间数据。本 PR 不操作生产库、不部署。

## 2. PR 变更记录

- **Flow 归属与交付。** `FlowStore` 与 Projects 读写显式 owner 标记；`DeliveryStore` 只修改 `delivery_*` 列。控制台、分配、评审和对账改读 Flow 行，历史 PR 仍按归档轮次查。启动迁移在替换视图前回填，不发布状态活动；四态离线脚本固定使用该步骤原有的控制台 SQL。用一次性 PostgreSQL 升级旧 schema 并再次启动，`console_facts` 与 `console_activity` 的快照保持一致，旧表不存在。`uv run pytest tests/test_flow_state_migration.py` 另覆盖错配归属回滚、账本重放和全部交付字段。将 Projects 归属查询临时替换为取最大 ID 后，`test_resuming_older_flow_selects_owner_even_after_both_flows_are_archived` 取到 2，预期为 1；保留显式标记。
- **Webhook 收件与登记。** `WebhookStore` 在同一行分别维护收件租约和业务登记时间。清理时删除未登记的过期收件，已登记的行仅清收件字段。旧库三种组合以及冲突回滚见 `tests/test_webhook_migration.py`。`uv run pytest tests/test_webhook_inbox.py -k receipt_expiration` 验证过期重投不重复创建 Flow；临时用 `state IS DISTINCT FROM 'done'` 判定可登记时重复创建了 Flow，用 `state='done'` 判断已登记时则把已忽略的投递误认成已登记，因此保留独立的 `recorded_at`。沿用原消费者行为：重投可以提示控制台重新同步，业务登记不会重复生效。
- **改为离线执行。** 不再随应用提交三项合表的数据升级代码或旧控制台 SQL。新版只创建最终表结构；发现任一旧表即在初始化前报错。四态迁移工具与配套测试留在 README 所列的固定旧版本。对 PostgreSQL 17 临时旧库执行单事务 SQL：不一致归属报错后原收件仍在；修复归属后执行成功。新代码连续启动两次，归属、交付、三类 Webhook 行、账本与控制台活动均已核对。生产库未操作。
