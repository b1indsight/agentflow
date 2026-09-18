# M5 F2：Webhook 持久接收与重放

> 状态：计划待审批，未实现。2026-09-18。
> 基线：main@origin / 55313e1f；M4 F2–F9 与收尾 #37 已合入对应分支链，尚未全部进入 main。
> 本计划独立以 main 为 PR base；实施前纳入实际需要的 M4 代码并重新核对，沿用同一 PR。
> 阶段范围与 F1 文档：[PR #38](https://github.com/b1indsight/agentflow/pull/38)。

## 1. 问题与结果

`app/control/api.py` 验签后调用 `spawn_handler()` 并返回 202，进程退出可能丢失已确认事件。
此外 `EventRouter.handle()` 对 issues 先独立 `record_event()` 再执行触发/取消；仅增加 inbox
仍可能在这两步间崩溃，重放时被 events 去重挡住。因此本 feature 同时修复接收和消费边界。

目标：合法事件在 202 前提交 PostgreSQL inbox；消费重试和重启不丢业务转换，不重复建 Flow、
入反馈或取消。保证“持久接收、可重试消费”，不宣称整个外部 GitHub 副作用恰好执行一次。

## 2. 数据与入口契约

新增 `webhook_inbox`，与业务 events 去重表分开：delivery_id 主键、event_type、payload、
原始 body 的 digest、received_at、state、attempts、next_at、claim_token、claim_expires_at、
processed_at、结构化 error_code。状态 pending/processing/done/blocked，claim 使用持久租约。
不保存签名 header、App secret 或其它认证信息；payload 按仓库数据对待，不进日志。

- 先有界读取原始请求体并验签，再解析 JSON/必需 header；chunked 请求也受限制。
  默认上限拟定 25 MiB，允许配置；超限 413，签名不符 401，缺失身份/非法 JSON 400。
- 在单事务中插入 inbox；相同 delivery_id、事件类型和 digest 返回 202，保留原进度。
  同 ID 不同类型/内容返回 409，记录不含 payload 的诊断，不覆盖旧数据。
- DB 提交失败返回 503；提交成功即使响应丢失，GitHub 同 ID 重投也不重复处理。
- 202 不等待 router、模型、GitHub API 或 worker；不得以后台任务内的 insert 冒充持久确认。

## 3. 消费原子性与恢复

新增有界批次消费者，按 received_at/delivery_id 公平扫描到期项；单 serve 仍使用行锁及
claim token，防止重复 wakeup/过期消费者覆盖新结果。崩溃后过期 claim 可重新领取。
默认单消费者、每批 50 项，有限退避 1s→60s（含 jitter）；达到 10 次进入 blocked，保留原因。

逐条审计 router 分支，将业务 event 预留、Flow/job/feedback 转换和 inbox done 合并进一个
共享 PostgreSQL 事务。issues 的 `record_event → on_trigger/cancel` 必须同事务。
已有 merge/review 事务改为接收事务视图或使用同一仓储入口，不能嵌套开启独立连接提交。
等待网络或模型不在事务内；调度通知仅在提交后发出，通知丢失由现有轮询恢复。
插件仍是提交后的尽力 hook，不计入 durable 业务保证，也不自动重放为“恰好一次”。

明确 ignore 的 bot/未知事件可完成为 done；格式错误或不满足处理契约的 poison event 标记
blocked，不能卡住后续 delivery。显式人工重试用既有 admin CLI 新增 inbox status/retry
子命令，仅作运维操作，不是部署 CLI；retry 校验终态/claim 并仅重置该 inbox 项。

已完成 payload 默认保留 7 天后清空，去重身份/digest 默认保留 90 天；未完成/blocked 不自动删。
业务 events 的 payload 保留须一起核对，不能只清 inbox 就声称事件内容已删除。
超过去重保留窗口不承诺历史 delivery 去重；列入运维说明。

## 4. 文件与实施顺序

- `app/webhook_store.py`、`app/webhook_models.py`：schema、状态、claim/ack/retry/清理；
  复用 `Store.transaction()`，由现有 schema 初始化接入。
- `app/control/api.py`：请求体边界、持久接收和返回语义。
- `app/control/webhooks.py`、`runtime.py`：消费者、唤醒、shutdown 和 claim 恢复。
- `app/control/router.py`、`service.py`、相关 review 仓储：提交边界调整，不重做业务算法。
- `app/config.py`、`app/cli.py`：有界参数及 inbox 运维入口。
- `tests/test_ingress.py`、新增 inbox PostgreSQL/故障用例及 router 回归；部署文档更新 202 语义。

先用 PostgreSQL 复现“202 后崩溃”和“events 先提交导致重放丢触发”，再实现仓储/入口，
最后接消费事务、重试/保留并验证与 M4 对账并发。处理函数执行和消耗预算须可测。

## 5. 验收与规模

- 真 PostgreSQL：重复并发接收、提交前后断连、claim 过期/迟到 ack、数据库短暂不可用。
- 对 issues 触发/取消、merge、评论/review/线程逐项注入业务提交前后失败，确认重放收敛；
  必须验证 events 已存在但 inbox 未完成的兼容情况，不只用 mock 验证“调用了一次”。
- 大请求/流式越界、相同 ID 不同内容、poison event、blocked 人工重试、清理边界。
- serve 重启后恢复未完成项，与 M4 对账及现有 results/effects 并发不重复副作用。
- Ruff、Mypy、构建及全量 PostgreSQL 回归通过；真实 GitHub 重投在 F3 记录证据。

预算目标约 1100 行手写增删；事务审计若超过 1200 行，拆“业务消费原子性”和“持久入口/消费者”
两个可独立审查的 feature，更新阶段表后实施，不省略原子性修复。
GitHub 不会自动重投失败 delivery；本 feature 不补收从未到达本系统的事件，见
[官方失败投递说明](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries)。
