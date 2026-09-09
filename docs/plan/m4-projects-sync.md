# M4 Feature 9/9：Projects 状态投影与恢复

> 状态：计划待审批；本 PR 只含文档，尚未实施。
> 规划日期：2026-09-09；基线：`main@origin` / `be65878f`（归档恢复修复 #27 已合并）。
> bookmark：`feat/m4-projects-sync`；实现前置：F3、F6、F8。
> 一 feature = 一 bookmark = 一 PR；前置合并后更新到最新 main，在本 PR 实施，不先合并纯计划。

## 1. 目标与依赖

依赖 F3 的有界周期扫描、F6 的 CLOSED 语义和 F8 的 Projects 接口。
把已追踪 Flow 的当前状态投影到原 Issue 的一张 Project 卡片，网络故障和重启后最终恢复。
看板是状态展示，不参与 job、归档成功、worker 释放或 ResultAccepted 的事务条件。

## 2. 状态映射

| 本地事实（从上到下优先） | 默认逻辑状态 |
|---|---|
| Flow SUCCEEDED | Done |
| Flow FAILED 或 CANCELLED | Blocked |
| ACTIVE 且 CLOSED、持续恢复错误或人工处理阻塞 | Blocked |
| ACTIVE 且 MERGED、尚未完成归档 | In Progress |
| ACTIVE、无 PR、INITIAL 尚未绑定 worker | Todo |
| ACTIVE 且有执行中/排队工作 | In Progress |
| ACTIVE 且 OPEN、无执行任务、等待 review | In Review |

逻辑状态映射到 F8 配置的 option ID，可把多个逻辑状态映射到同一选项。不新增 FlowState。
恢复中的短暂退避不立即映射 Blocked；仅已有明确失败/人工阻塞事实触发该状态。

## 3. 持久投影与远端恢复

- 新增小型 `project_projections` 表，以 destination project + 原 Issue content ID 唯一，
  保存负责的 Flow ID、item ID、期望状态版本、已确认版本、next_at 和脱敏错误。
  同一 Issue 的多次 Flow 共用一张卡，由当前 ACTIVE Flow 优先、否则最新 Flow 驱动，
  历史 Flow 的迟到扫描不得覆盖新 Flow。
- 周期从数据库事实重新计算期望状态，只更新变化的投影。需要扫描 ACTIVE 和尚未同步的
  终态，并可发现刚完成的 Flow；不能用 `active_only=True` 使 Done 永远丢失。
  默认仅为启用配置后新建/仍活跃的 Flow 建卡，已有投影的终态持续补偿，历史批量回填另做。
- 此表记录可替换的展示期望值。现有 ResultPlan/effect ledger 绑定 worker job 和 ACK，
  不适合终态后独立更新看板；不为 Project 伪造 job 或扩张归档结果协议。
- 先按 content ID 查 item；确认缺失才 add；响应丢失先查询，无法查全时保留待确认。
  set Status 前比较远端值，相同则确认，否则写入；写后丢响应下轮复读。
- 每个目标 item 仅一个本地在途写。发送前复核最新期望版本，迟到响应只确认其自身版本；
  扫描发现更新目标后继续追赶，不能把旧响应记成已同步最新状态。
- 重启后重新读取 item/Status；429/临时故障持久退避，权限/配置错误显示阻塞。
  不自动重建被人工删除/归档的已有卡片；显示原因，由 `project retry <repo>` 在条件修复后
  重新核对。人工改动受管 Status 会被下一轮当前状态覆盖，其他字段不受本功能管理。
- Project 故障不改变 Flow 生命周期，不阻止 M3 Done/释放/ACK；禁用配置立即停止新的
  Projects 请求，保留诊断和既有卡片，不发送删除操作。

## 4. 落点与实施顺序

| 落点 | 改动 |
|---|---|
| 新增 `app/project_store.py`、`app/control/projects.py` | 投影记录、状态计算及两步恢复 |
| `app/store.py`、`control/runtime.py` | schema 与独立有界扫描 |
| `app/cli.py` | 投影状态、错误及显式重试 |
| 新增 `tests/test_project_sync.py`，既有 archive/remote E2E 测试 | 响应丢失、乱序与归档隔离 |

先写状态映射和真实 PostgreSQL 的旧版本响应测试，再接 F8 查询/add/set；
最后验证归档成功与 Project 离线可并存，并完成阶段真实验收矩阵。

## 5. 验收

- 各状态映射正确，原 Issue 只有一张卡；同一 Issue 新 Flow 覆盖旧 Flow 的展示权。
- add/set 成功但响应丢失、重启、重复扫描不新增重复卡或重复无效写；旧响应不能确认新版本。
- Done、Failed、Cancelled 终态仍可同步；Project 离线时 M3 归档照常成功和释放。
- 用户改其他字段被保留；删除/归档卡片不会被自动反向操作，显式重试先核对。
- 实施后用授权测试仓库与组织 Project 完成两条 Flow 并发、漏 webhook、暂时失败、重启、
  close/reopen、merge/archive→Done 的真实 smoke，并记录每项证据和遗留项。

## 规模预算与拆分触发点

按相对合并基线的新增行 + 删除行估算：产品 460、测试 450、文档 110，合计约
**1020 行**。数字是规划预算，不是已实现统计；共享测试替身、schema、格式调整也计入。
目标 800–1100 行，预计超过 1200 行先缩小/拆出独立可验收 feature，不压缩测试或代码行数凑数。
优先另拆：双向拖卡控制、历史全量导入、额外字段和每 Flow 新卡片。未完成必需正确性验收时不能仅以行数达标合并。

本次规划仅做文档内容、链接和 diff 检查。实施按“先失败测试→最小实现→针对性验证→完整检查”
推进；完整检查为实际 PostgreSQL 下 pytest、Ruff、Mypy 和 `uv build --no-sources`。

