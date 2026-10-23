# 取消、drain 与停机

取消是持久的停止请求，不能撤销已经推送的 commit、已创建的 PR 或评论。
`flowtool flow cancel <flow_id>`（也包括移除触发标签）将 Flow 标记 CANCELLED、退休 queued
工作；已分配或状态未知的任务留下 stop intent 和 binding。输出 `stop_pending=True` 表示尚未
确认停止。离线、lease 到期、serve 重启都不能解除这一占用。
发出 Assignment 前另存 dispatch authorization；Started ACK 丢失后不再将任务重排为
“从未发送”，对应未知执行保留绑定并等待原 worker/journal。

## 停止确认

同一鉴权 WebSocket 使用 v15 的 StopExecution / ExecutionStopped / StopAccepted 消息。
请求绑定 flow、worker、job、assignment token、binding 创建时间、当前 epoch 和独立 request ID。
worker 必须持有匹配 journal，当前执行只能由持有该 task / 子进程对象的进程取消。
每次 assignment 在启动前持久化执行 nonce；停止凭据包含 nonce，原 journal 继续保存直到 ACK。
ACK 丢失后重连重发同一凭据，控制面按 digest 幂等确认；不根据旧 PID 杀进程。

重放旧 stop 时仍须对账控制面已派发的下一任务。若原 worker 返回的 journal 未记录该任务，
只有未 dispatch 的准备任务可重排；普通已 dispatch 任务记录明确失败，不自动重新运行。
CLOSED/MERGED 的缺失 journal 仍保留原绑定，等待人工核验交付事实。

执行退出后先持久保存停止证据，再清理原 workspace 的未提交修改和未跟踪文件，保留目录与
ignored 缓存并归还空闲池；copy 和 worktree 都在原路径复用。清理成功后才发送停止凭据，
不增加消息字段、数据库阶段或目录删除。失败保留原 journal 和控制面绑定，重连可重试；
未知归属、锁文件和无法清理的 VCS 操作仍阻塞供人工核验，不强行接管或删除元数据。
停止 ACK 保留 journal 的原位置证明，取消期间已知合并时仍可使用它完成 ARCHIVE。
再次分配时递增位置代次，旧停止凭据不得清理新任务。已知合并前后的绑定决定仍由原控制面
停止确认处理；取消不撤销远端 commit/PR。入口与权限优化由 [#108](https://github.com/b1indsight/agentflow/issues/108) 跟踪。

worker 等待当前 coroutine 退出；Codex 和 VCS 的现有取消路径会 SIGKILL 已拥有的进程组并
等待直接子进程退出，不增加 10 秒 TERM 等待窗口。Amp 先 SIGTERM CLI、有界等待后升级
清理事前捕获的独立子进程组；无法保证任意脱离进程树的进程被回收。停止处理
最多等待执行 task 15 秒，服务端停止握手总预算 20 秒；超时留下请求/journal，不确认停止。
只适用于可信执行代码的普通子进程；主动脱离进程组的恶意代码不在当前隔离保证内。

已有执行在 worker 进程崩溃后只有 started journal、没有存活执行对象和停止凭据时，恢复保持
blocked。恢复原 journal 不等于证明旧进程结束。不要删除 journal、改 nonce 或直接写 confirmed。
先隔离原运行环境、检查原进程及远端操作事实，再制定人工恢复步骤；此版没有 force-release。

控制面还会等结果和已 dispatch 的外部 effects 收敛。取消后只核对已发送写操作，不重发；
结果未知就保留占用。effect 标记 skipped 时，只要已发送且没有 receipt，仍阻止
释放。取消期间确认 PR 已合并时，保留原 binding，符合条件的原 Flow 转回 ACTIVE 只做 archive；
不会重新运行编码。如果另一条 Flow 已占用同一 Issue，保留现场并人工处理冲突。

## Drain

```sh
flowtool worker-drain worker-1
flowtool workers
# 需要恢复接受新 Flow 时：
flowtool worker-drain worker-1 --undo
```

这是现有 worker 的运维控制，不是部署命令。draining 位存 PostgreSQL，跨 reconnect/serve
重启保留。drain 只停止分配新 Flow，已有 binding 的 revision/archive 继续运行。
`workers` 显示 DRAINING、FLOW、JOB、连接和恢复诊断；draining 且无 FLOW/JOB 才适合更新。
等待人工 review 的 Flow 可长期占用；需要立即退出时，对具体 Flow cancel，再等停止确认。
不要通过 undrain、重建 Pod、改 worker ID 或超时释放绕过停止确认。

## SIGTERM 与升级

serve 用 Uvicorn 的 20 秒 graceful shutdown 窗口停止接收并收敛请求，后台短事务在 lifespan
退出时完成/取消；持久 inbox/result/stop intent 保留供下次启动恢复。worker SIGTERM 停止连接，
取消当前执行并回收其已拥有的子进程。停机不是业务成功，也不是自动停止确认：只有控制面的
STOP 请求和有效凭据握手能完成取消。未写出凭据的进程崩溃依然按未知执行处理。
容器 stop grace / Kubernetes terminationGracePeriod 至少 45 秒，超时 SIGKILL 只靠持久事实恢复。

当前仅支持协议 v15 / journal v4 / workspace manifest v1，serve/worker 必须使用同一版本；版本定义见
[协议](../../app/contracts/protocol.py)与 [WorkerSession](../../app/worker/state/session.py)。仓库未部署，未保留跨版本
数据库或 journal 迁移。当前版本停机重启仍应先 drain，再保存数据和 worker root/HOME；
按原身份启动并核对恢复状态后 undrain。不要通过编辑 journal 版本号绕过验证。

## 验证范围

自动化覆盖 PostgreSQL 停止 fence、超时不释放、drain 持久性、合并竞态、未知 effect、完整
双向 worker 协议，以及真实无害父/子进程退出和 ACK 丢失后新 client/journal 重放。
网络重连不会把“没有本地 task”当作停止证明。F3 真实 GitHub/模型取消验收、整套部署中的
serve/worker 分进程故障注入尚待运行环境，不能用上述单机测试代替发布验收。

未 dispatch 的准备任务取消后不等待 worker journal；本地未应用结果仍须先收敛。已确认停止
可在 Flow 重绑后再次 ACK，历史 ACK 不释放新 binding。取消期间找回的创建 PR 会记录其身份
与 merge 事实；已经合并时仍在原 binding 上完成归档，不因为取消丢失 PR 对账入口。
