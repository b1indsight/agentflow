# M4 Feature 1/9：同秒反馈版本去重

> 状态：用户于 2026-09-09 批准按序实现并推送；F1 已实施并验证，留待用户统一 review/合并。
> 规划日期：2026-09-09；基线：`main@origin` / `be65878f`（归档恢复修复 #27 已合并）。
> bookmark：`feat/m4-feedback-replay`；实现前置：最新 main（M3 F1–F3 与归档恢复修复已合并）。
> 一 feature = 一 bookmark = 一 PR；用户统一 review/合并，后续 PR 以已实现前项分支为基线。

## 1. 目标与边界

修复 M2 已记录的“远端观察 A→B，迟到 A→B webhook 入队，同秒 B→A 后重放旧 B”问题。
只调整反馈版本与待处理编辑的判重；保留真实新编辑、乱序编辑、删除终态和冻结批次。
此项是 F4/F5 扩大远端观察频率的前置，不把 M2 真实验收标成完成。

## 2. 方案

- 复用 `review_versions`、`review_pending_edits` 和现有 Flow 事务锁。优先从版本历史识别
  已表达的编辑转换；只有历史无法表达来源时才增加最小来源字段，不建立第二套事件库。
- 判重依据包含对象身份、时间戳、前后内容和已观察版本范围。不能永久按正文或 A→B 指纹去重：
  同一正文在较晚时刻再次出现可能是真实新编辑。
- 远端查询先冻结本地 item/version，回写时重新核对；较旧查询不得覆盖期间提交的 webhook。
  同秒转换已在该历史区间表示时，迟到 webhook 只记录接收事实，不派生新的待处理版本。
- 同秒内真正重复编辑与迟到重放无法区分时，保留诊断并等待新鲜远端观察；不猜测顺序，
  不仅按 delivery ID 排序重放，也不把无法确定的编辑标成已处理。
- 删除保持不可复活；回滚必须同时撤销 delivery 接收、版本写入、pending edit 变更和 settle。
  不改变 Judge context、revision_cycles、wire protocol 或已冻结版本的含义。

## 3. 落点与实施顺序

| 落点 | 改动 |
|---|---|
| `app/review_store.py` | 历史转换核对、pending edit 收敛和 observation 版本检查 |
| `tests/test_review_threads.py`、`test_review_batches.py` | 原缺陷与真实新编辑的 PostgreSQL 回归 |
| `tests/review_memory.py` | 仅更新受影响的既有替身 |
| `docs/architecture/README.md`、M2 F4 计划 | 实施通过后更新缺陷状态和证据 |

审批后先复现已知交错，再实现历史判重，随后补并发/回滚，最后更新状态记录。
本 PR 同时携带 M4 阶段总表及入口链接；其他 feature 的计划和代码各自留在独立 PR。

## 4. 验收

- 原复现最终正文保持 A；不会生成新的旧 B 反馈、唤醒 settle 或阻止正确 resolve。
- 新时间戳的 A→B→A→B、不同对象相同正文、乱序真实编辑仍可逐次入库。
- 同秒不能定序的记录不派生错误修订；后续新鲜观察可以收敛，重启不丢诊断。
- webhook 与 observation 跨连接并发、事务故障、重复 delivery、删除后迟到编辑保持一致。
- 聚焦上述两个测试文件；再执行阶段总表规定的 PostgreSQL 完整回归及质量检查。

## 规模预算与拆分触发点

按相对合并基线的新增行 + 删除行估算：产品 300、测试 370、文档 180，合计约
**850 行**。数字是规划预算，不是已实现统计；共享测试替身、schema、格式调整也计入。
目标 800–1100 行，预计超过 1200 行先缩小/拆出独立可验收 feature，不压缩测试或代码行数凑数。
优先另拆：历史诊断 CLI 与批量修复工具。未完成必需正确性验收时不能仅以行数达标合并。

本次规划仅做文档内容、链接和 diff 检查。实施按“先失败测试→最小实现→针对性验证→完整检查”
推进；完整检查为实际 PostgreSQL 下 pytest、Ruff、Mypy 和 `uv build --no-sources`。

## 实施记录（2026-09-09）

- 使用既有版本历史核对同一对象、同一时间戳的前后正文转换；迟到或无法区分的新一轮重复
  编辑保留在 pending edits，等待冻结 item/version 的新鲜远端观察，不派生旧反馈。
- 同一前序存在不同后继正文时不按 delivery ID 猜测顺序。较晚时间戳的真实重复编辑仍正常入库。
- 不修改 schema、worker 协议、冻结批次或修订轮数；诊断记录沿用既有 pending edits。
- 新增 `tests/test_feedback_replay.py`：原复现先得到 5 failed / 2 passed，再实现修复。
  已有乱序编辑和线程恢复聚焦回归 85 passed；补充首次远端观察、跨连接并发与删除回归后执行完整检查。
- 最终实际 PostgreSQL 全量：512 passed / 0 skipped；Mypy、Ruff 和 sdist/wheel 构建通过。
  仅有既有 Starlette 弃用警告；真实 GitHub/Codex/bub 验收仍待进行。

## 2026-09-10 恢复问题返修

用户已明确授权修复全部可能卡住、无法恢复或旧状态重放的问题。本项补齐远端观察跳过中间编辑
的覆盖规则，以及直接 webhook 与队列共用的冲突判断。两个 PostgreSQL 回归用例先失败后通过。
