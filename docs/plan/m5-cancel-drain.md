# M5 F6：运行中取消、drain 与停机

> 状态：已批准并实现，自动化验证及独立审查进行中。2026-09-20。
> 文档 PR 基线 main@origin / 55313e1f；真实验收依赖 F3，实施前集成 M4 lifecycle/recovery。
> 阶段范围：[PR #38](https://github.com/b1indsight/agentflow/pull/38)。

## 1. 问题与不变量

`ControlService.cancel()` 当前标记 CANCELLED、取消 jobs 并释放 binding，但未停止 worker
执行。`WorkerClient._cancel_execution()` 和 Codex driver 已有 task cancel/killpg 路径，
却没有持久的控制面停止请求及确认。原进程仍在修改 repo/push 时不可复用该 worker。

取消是“请求停止并收敛”，不撤销已经提交的远端 push/PR/评论。Flow 业务终态、执行停止
与槽位释放分别记录，不能用 CANCELLED 一个值代替三者。所有取消入口（CLI、标签移除、
其它已实现自动取消）走同一事务路径；已 merged Flow 继续原归档规则，禁止强行释放。

## 2. 持久停止协议

新增 execution stop intent：flow/binding/job、assignment token/epoch、request_id、reason、
requested_at、state、confirmed_at 和停止凭据 digest，默认依附现有 binding/assignment，
不新建独立任务调度器。queued 且可证明从未派发的任务可直接取消；其余请求必须保留占用。

新增版本化 StopExecution/ExecutionStopped 消息，复用现有鉴权 WebSocket，严格匹配
worker identity、当前 epoch、assignment token 和 request_id。旧 worker 明确协议不兼容，
不靠忽略消息继续接新任务。重连重发请求，worker journal 持久记录已停止凭据并重复回复，
控制面提交确认/释放后 ACK；ACK 丢失不得导致第二次释放或误删新任务数据。

worker 收到请求后停止新的 tool RPC、取消当前 coroutine，等待其 CodeAgent/VCS/Judge
子进程退出。Codex 已有 killpg 路径要接入统一执行所有权，不能只 cancel Python task。
先给已拥有进程组有限终止窗口（拟 10 秒），之后强制结束并等待 reap；适用子进程全部
退出才报告 stopped。确认时仍未知的进程或外部操作保持 blocked，不假定停止成功。

进程所有权凭据包含本次执行 nonce、启动身份和进程组关联；裸 PID 不作跨重启杀进程依据。
worker 自身崩溃后只能在可证明拥有旧执行的情况下终止/确认；否则保留占用并给出人工
核验步骤。首版仍是 trusted-host 模型，不宣称能强制约束主动脱离进程组的恶意代码。
如果可靠 stop 需要新增 supervisor，先拆前置 feature，不能把“不再有 coroutine”当证明。

## 3. 竞态及结果收敛

- 停止事务与 result apply 在同一个 Flow/binding 锁下定序；已经提交的成功交付不能回滚。
- stop 先提交：后续结果可保留为诊断/已发生事实，不重新激活工作流或触发新修订；
  已经发出的远端操作由既有 effects/result 恢复到可确认状态，再决定释放。
- result 先提交：取消只作用于当前剩余执行，不能把已完成 assignment 的凭据用于新 assignment。
- worker 离线、lease 超时、serve 重启均不等于 stopped；禁止超时自动解除 stop-required 占用。
- merge/archive 与 stop 并发按现有持久 merge 事实判断，归档清理仍需原 worker 凭据。

## 4. Drain 与进程退出

新增运维 `worker drain <id>` / `worker undrain <id>`（名称实现时与现有 CLI 分组一致）：
只管理现有 worker 运行状态，不是部署 CLI。drain 状态持久化，在 reconnect 后保留。
默认 drain 停止给该 worker 分配新 Flow；已绑定 Flow 的后续 revision/archive 继续运行至释放，
状态展示 draining/bound/idle。长时间等待人工 review 可阻止自然 drain，必须明确展示。
需要立即停止时由用户对具体 Flow cancel，不让 drain 隐式取消业务。

SIGTERM 处理采用有界 shutdown：serve 停止领取新工作并完成当前短事务，关闭连接前保留
inbox/result/stop intent；worker 停止接任务，回收或明确记录当前执行，持久 journal 后退出。
超出窗口退出只能留下 recovery-required，不能伪造 stopped。K8s terminationGracePeriod
必须大于应用停止窗口，由 F7 配置。SIGKILL 只依靠 journal/stop intent 恢复，不承诺优雅停止。

## 5. 文件与实施顺序

`app/control/service.py`、`gateway.py`、`coordinator.py`、Store binding/job 接口；
`app/protocol.py` 及 worker client/journal/runtime；`app/codex_driver.py` 与 VCS 子进程边界；
`app/cli.py` 的运维入口和 `docs/operations/cancel-drain.md`。
新增 PostgreSQL 竞态测试、真实无害子进程停止测试、协议/journal 重放用例。

先复现“cancel 后槽位过早释放” → 持久 intent/协议 → worker 停止凭据与进程测试 →
结果/merge 竞态 → drain/信号收尾 → serve/worker 分进程故障验收。

## 6. 完成标准与拆分阈值

运行中取消实际终止无害测试进程及其普通子进程；STOP/ACK 任意边界重启不双释放；旧 epoch
和 PID 复用不误杀；离线保持占用；queued 与正在交付的区别正确；drain 跨重启保留且已绑定
Flow 可收尾；超时和缺 journal 给出 blocked 事实。标准 lint/type/build 及全量 PG 回归通过。
真实取消复验使用 F3 测试仓库，不在用户生产 Flow 上故障注入。

预算目标约 1100 行，但此 feature 跨协议/持久层/进程边界，预计超过 1200 行时必须在实施前
拆成“持久取消与停止确认”和“drain/优雅停机”；需要 supervisor 再单拆，不以弱化停止证明
满足行数。停止确认是 F7 安全更新前置；跨 worker 迁移和强制释放未知执行不在范围。

## 实施记录

协议 v9；`worker-drain <id> [--undo]`；行为、升级限制及证据见
[取消和停机说明](../operations/cancel-drain.md)。复用现有 Codex/VCS 所有权及 SIGKILL
进程组路径，停止 task 确认预算 15 秒，握手 20 秒，没有新增跨重启 PID 终止逻辑。
未知旧进程保持 blocked；真实环境的 F3 取消和整套部署故障注入仍待执行。

2026-09-22 授权返修三项：
- 未 dispatch/未 started 的 preparation 不要求 worker 停止凭据，取消时退休未发送 job 并释放
  当前 worker job 槽；旧 preparation 回调仍被 job/token fence 拒绝。若控制端结果未应用，
  取消绑定保持到结果/effects 收敛后再释放，避免要求不存在的 worker journal。
- 已 confirmed 的停止回执按原 request/job/token/nonce digest 和当前连接 epoch 重放确认；
  不再要求 Flow 仍使用旧 binding，不触碰已重绑的新任务。未确认停止仍严格校验原 binding。
- 取消时恢复 OPENING PR，先只读核验 repository/head/base，再在 workflow 锁内保存 PR 身份、
  URL 和当前 open/closed/merge 事实。merge 经原 record_merge 进入保留绑定的 archive 路径；
  版本变化或异仓库观察继续阻塞，不补发远端写入。

三个原缺陷均有先行失败 PG 回归；扩展覆盖开放/关闭/合并/异仓库 PR、旧 nonce 伪造、
重绑后新任务未被修改，以及控制端准备结果收敛。归档原子释放回归防止把取消的 pending
结果保护误用于正常 archive 提交。沿用既有停止/结果/归档机制，无新增强制释放入口。
验证：791 项 PostgreSQL 全量、Ruff、Mypy、构建通过。准备任务直接退休仅限 CANCELLED，
保留 PR close/pause 与 reopen 的原恢复语义；58 项取消/暂停/准备重试定向回归通过。
