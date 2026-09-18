# M5 F3：真实业务闭环验收

> 状态：计划待审批，未实现/未执行真实验收。2026-09-18。
> PR 文档基线：main@origin / 55313e1f；实际运行依赖 M5 F1/F2 以及已集成的 M1–M4。
> M4 后续代码仍在分支链，实施前核对 main；本计划不把替身测试当成真实服务证据。
> 阶段范围与部署文档：[PR #38](https://github.com/b1indsight/agentflow/pull/38)。

## 1. 交付与边界

交付可重复运行的 opt-in 验收脚本、独立测试仓库 fixture、分阶段 runbook 和脱敏结果记录。
验证真实 GitHub App、Codex、Judge、PostgreSQL、serve/worker 及 Cloudflare 部署链路，
首次真实 Issue→PR→review→merge→archive 在 Docker/K8s 之前完成。
不新建部署 CLI，不把 Cloudflare 包装成应用功能，不用 stub 补齐外部调用后记为通过。

使用专用测试仓库、单 worker、固定版本和小型编码任务；只允许受控 fixture 路径。
脚本必须显式指定 repo、run-id 和可写模式，默认只检查现有状态/生成操作清单。
创建 issue/评论等操作只在显式 allow-write 且测试 repo allowlist 匹配时执行，记录所有资源 ID。
人工 review、merge 仍由用户在 GitHub 完成，不自动合并，也不替用户提交审查意见。
重复运行复用 run-id 标记，先查已有 issue/PR，远端结果未知先对账，不能盲目重复创建。

## 2. 前置检查和场景矩阵

运行前确认 GitHub App 权限/安装、labels/allowlist/reviewers、worker 工具链和模型配置，
确认专用账号隔离、DB 连通、worker 注册及预算/超时。必要时仅返回缺项，不输出凭据。
当前 App smoke、model smoke 的结果分别记录，连接成功不替代语义场景通过。

| 阶段 | 操作 | 必须保存的通过证据 |
|---|---|---|
| 接入 | GitHub 真实 delivery 与一次同 ID redelivery；错误签名及私有路由检查 | 202/401/404、inbox 状态、唯一 Flow |
| 首轮 | allowlist 用户触发，agent 完成小型任务并交付 PR | 同一 Flow/repo/branch、commit SHA、PR、远端核验结果 |
| 修订 | 两轮明确 feedback，第一轮完成后再发送第二轮 | 批次身份、两次新 SHA、会话处理记录、线程回复/resolve |
| 语义分流 | 单独提出 answer、wait/ignore 场景，保留输入和判定 | 非修订不新增提交；无重复回复，模糊判定记未通过而非改 fixture 掩盖 |
| 合并归档 | 用户人工 merge | 唯一归档、状态标签/评论、原 worker 清理凭据、binding 释放 |
| 恢复 | 在测试环境重启 serve，短暂断开 worker，重投事件 | inbox/result 重放收敛，原执行未重跑、不重复 PR/评论 |
| M4 补验 | 漏失已追踪 PR webhook、close/reopen、配置了 Projects 时检查看板 | 对账恢复、暂停/恢复、受管状态最终一致；缺 Projects 配置明确未验收 |

真实 session fallback 不靠破坏未知会话数据强行触发；在测试专属环境使用可恢复的缺失 session
fixture，先证明未执行，验证现有一次性 fallback。如果 provider 无法稳定构造则记录未覆盖。
进程被杀、备份恢复和运行中 cancel 的完整矩阵留给 F6/F8，F3 不依赖它们阻塞首轮联调。

## 3. 脚本、数据与证据

- `scripts/smoke/live_flow.py`：分阶段检查/有限写入，明确超时、poll 间隔、run-id 和恢复点。
  通过现有 GitHub adapter/CLI 获取事实，不复制完整业务编排或直接修改业务表。
- `tests/fixtures/live_flow/`：小型可审阅任务与期望，不放真实仓库代码、secret 或模型输出。
- `tests/test_live_smoke.py`：仅测试脚本自身边界、幂等资源发现、失败退出、脱敏和超时；
  不将这些测试命名为“真实 E2E 通过”。
- `docs/verification/m5-live-e2e.md`：逐步操作、人工动作、通过/失败/未执行记录模板。
- `docs/deployment/README.md` 和架构状态入口：只根据实际证据更新。

结果包含应用/协议/工具版本、UTC 时间、run-id、repo/issue/PR/delivery/job IDs、SHA、
每阶段状态与证据链接；不保存 token、认证 header、完整 prompt 或私有日志。
原始输出保存在测试专属私有目录，仓库只纳入人工审阅后的摘要和公开测试资源链接。
默认等待总预算 30 分钟、模型任务沿用现有 timeout，超时返回非零并说明最后可确认的阶段。
脚本退出不自动删除 Flow repo/journal；测试资源保留用于排查，清理另有显式步骤。

## 4. 实施顺序与验收

先核对已集成基线和部署文档 → 脚本边界失败测试 → 检查/记录入口 → 真实首轮 → 两轮反馈 →
人工合并归档 → 重启/重投与 M4 补验 → 更新实际证据和剩余待办。
真实运行发现产品缺陷按规模就地修复或单独修复 PR，保留失败记录，重新执行相关阶段。

交付验收：脚本帮助和 dry-run 可用；写入范围/幂等/超时/脱敏自动化通过；完整矩阵标明
真实结果。没有测试 repo、App 安装、模型认证或人工合并结果时，只能报告工具已交付，
F3 真实闭环仍未完成。普通 PR CI 不使用外部模型密钥或自动写真实仓库。
实现代码运行 Ruff/Mypy/适当 pytest/构建；产品修复增加对应 PostgreSQL 回归。

预计约 850 行手写增删；超过 1200 行优先拆验证工具和额外故障场景，不能把真实核心闭环
降级为 stub。与 F1 文档及 F2 的依赖在同一 PR 实施前 rebase，不另开“实现 PR”。
