# Webhook 持久接收

验签及请求体校验通过后，`/webhook` 在 PostgreSQL inbox 提交成功才返回 202。
默认请求体上限 25 MiB，可通过 `FLOWTOOL_WEBHOOK_MAX_BODY_BYTES`（1–100 MiB）调整。
相同 delivery/type/body 重投不重置进度；同 ID 不同内容返回 409，数据库写失败返回 503。
413 表示超过读取上限，400 表示缺少身份或非法 JSON。隧道配置仍只公开 `/webhook`。

消费者按批公平领取，业务转换、events 去重和 inbox done 同事务；失败回滚后退避，
最多尝试 10 次，缺失/非法业务字段直接 blocked。消费者通过线程运行数据库工作，
通知回到事件循环；停机等待当前数据库工作结束。进程崩溃后 60 秒 claim 到期可恢复。
插件通知是提交后的尽力操作，不保证跨崩溃执行；任务调度由轮询补偿。

在 control 配置环境运行：

```bash
uv run flowtool inbox status --limit 50
uv run flowtool inbox retry DELIVERY_ID
```

status 仅显示身份、状态、次数、下次时间和错误类别，不输出事件正文。
retry 仅重置保留 payload 的 blocked 项，不复活 done、不抢占 processing。
先排查并修复错误，再 retry；字段错误需要核对源事件，不能修改已接收正文掩盖身份冲突。

`LegacyDelivery` 表示 inbox 之前已存在 events 去重记录，无法证明旧业务是否完整执行。
自动重放会继续 blocked，必须人工核对 Flow/job/远端事实后单独修复；不要删除 events 强行
重试或生成新 delivery ID，否则可能重复触发业务。原有业务事件本身不会因升级主动重放。

done payload 默认 7 天后清空，相关 events 正文同时清空；inbox 身份/digest 保留 90 天。
pending/processing/blocked 不自动清理，清理每轮最多 500 项；既有 events 去重身份继续保留。
这是固定首版策略，不等于所有 review/result 数据的统一保留策略。

数据库提交成功但 HTTP 响应丢失可安全重投。从未到达系统的事件仍需在 GitHub 核查补投，
不能依赖当前服务或 Cloudflare 恢复这类数据。生产可用不代表真实业务闭环已验收，见 M5 F3。

同一仓库按持久接收序号消费；退避、processing 或 blocked 的旧记录会挡住后续记录，
防止取消先完成后又被旧 trigger 复活。其他仓库继续处理。仓库 blocked 应优先诊断，
不能通过跳过/删除原始记录恢复吞吐；未知业务事实仍需人工核对。该保证不纠正上游乱序投递。
