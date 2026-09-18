# M4 Feature 2/9：多 Flow 恢复并发与调度隔离

> 状态：用户于 2026-09-09 批准按序实现并推送；F2 已实施并验证，待用户 review/合并。
> 规划日期：2026-09-09；基线：`main@origin` / `be65878f`（归档恢复修复 #27 已合并）。
> bookmark：`feat/m4-recovery-concurrency`；实现前置：最新 main；独立于 F1。
> 用户统一 review/合并；本 PR 以 F1 分支为 base，只展示 F2 增量，不等待前项合并。

## 1. 目标与已有能力

多 identity、公平领取、Flow 独占 worker/repo 和同 Flow 批次串行已实现。本项补恢复路径：
`recover_pending_results()` 当前逐 job await，且 `_sweep_assignments()` 等待它后才执行 settle。
一个慢 GitHub 请求可能拖延其他 Flow 的结果恢复和后续 lease sweep，不需要重建调度器。

## 2. 方案

- 把持久 lease/assignment sweep、settle sweep 与网络结果恢复分开驱动；失败各自记录并继续。
  保留单 serve 模型，shutdown 统一停止并等待这些任务。
- 结果恢复按可配置的小上限并发，默认 4；每轮只领取有界数量，按稳定顺序公平续扫。
  使用固定数量任务或有界分批，不能为全部历史结果一次性创建无限 asyncio Task。
- 同一 job 继续使用 `_result_lock(job.id)`，同 Flow 的状态与 effects 继续依赖数据库锁和
  现有 pending result 约束。禁止把进程锁误当成多 serve 协议。
- 周期任务不重叠启动同一轮恢复；启动恢复与 WebSocket 终态回传复用已有排他路径。
  一个 Flow 超时、ResultPending 或失败不取消其他 Flow。
- 避免大范围重构 Gateway、Store 和 ResultApplication；不改变 SDK 写重试语义、
  assignment token、connection epoch、原 worker binding 或 ARCHIVE 释放条件。

## 3. 落点与实施顺序

| 落点 | 改动 |
|---|---|
| `app/control/runtime.py` | 本地 sweep 与网络恢复独立生命周期 |
| `app/control/gateway.py`、`app/store.py` | 有界恢复批次、公平续扫和同 job 排他 |
| `app/config.py` | 恢复并发上限与合法值校验 |
| `tests/test_gateway.py`、`test_remote_e2e.py`、`test_config.py` | 慢请求隔离、并发上限与停止 |

先以可控 Event 阻塞 Flow A 的外部请求，验证 B 和 lease/settle 应继续推进；再分离循环并
加入有界恢复。用现有连接和数据库测试设施，不另建压测框架。

## 4. 验收

- A 的网络请求未返回时，B 可应用结果，C 的 lease 到期和到期反馈批次可以推进。
- 达到并发上限后无额外网络恢复；待处理数超过批次上限时后排 job 最终被处理。
- 同 job 的即时回传、启动恢复和周期恢复交错，只应用一次并按原协议 ACK。
- 两个 worker 同仓库不同 Flow 仍使用独占目录；同一 Flow 不同时派发两个执行任务。
- shutdown、恢复异常、连接重注册后不泄漏任务，旧 epoch 不能提交新结果。
- 聚焦 gateway、remote E2E 和配置测试，再运行阶段规定的完整检查。

## 规模预算与拆分触发点

按相对合并基线的新增行 + 删除行估算：产品 360、测试 480、文档 100，合计约
**940 行**。数字是规划预算，不是已实现统计；共享测试替身、schema、格式调整也计入。
目标 800–1100 行，预计超过 1200 行先缩小/拆出独立可验收 feature，不压缩测试或代码行数凑数。
优先另拆：性能压测平台、调度算法更换和通用任务池。未完成必需正确性验收时不能仅以行数达标合并。

本次规划仅做文档内容、链接和 diff 检查。实施按“先失败测试→最小实现→针对性验证→完整检查”
推进；完整检查为实际 PostgreSQL 下 pytest、Ruff、Mypy 和 `uv build --no-sources`。

## 实施记录（2026-09-09）

- lease、settle 和网络结果恢复各有独立周期任务；单项失败不停止其他任务，shutdown 取消并等待全部任务。
- 固定数量的 TaskGroup 消费有界结果页，保留实时结果/恢复共用的 job lock；重复 sweep 串行领取。
- 每轮扫描冻结最高结果 ID，分页推进后回绕；持续到达的新结果不会无限推迟旧 pending 结果。
  游标仅影响本进程扫描公平性，重启重新从持久 inbox 读取，不改变结果提交和 ACK 语义。
- 增加 `FLOWTOOL_RESULT_RECOVERY_CONCURRENCY`（默认 4，上限 64）与
  `FLOWTOOL_RESULT_RECOVERY_BATCH_SIZE`（默认 64，上限 1000），下限均为 1。
- 新增测试先复现 10 项失败；另以持续新结果测试复现旧结果不能及时回绕，再补有限扫描边界。
  覆盖慢请求、pool 上限、取消回收、同 job 排他、分页与数据库边界。
- 聚焦回归 48 passed；实际 PostgreSQL 完整回归 523 passed / 0 skipped；Ruff、Mypy、
  sdist/wheel 构建通过。仅有既有 Starlette 弃用警告，真实外部服务 smoke 未执行。
