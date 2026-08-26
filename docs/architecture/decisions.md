# 关键决策、风险与已知短板

## 6. 关键决策 & 推荐

| 决策 | 结论 | 说明 |
|---|---|---|
| 认证 | ✅ **GitHub App** | 独立 bot 身份；接入见附录 A |
| 合并 | ✅ **全程人工** | 你在网页 Approve + Merge，工具只监听合并事件收尾 |
| CI 门禁 | ✅ **交给仓库自身 CI** | 依赖 repo 的 CI + 分支保护呈现在 PR 上；工具不自建门禁，人工合并时你据此判断 |
| 合并方式 | ✅ **由你在网页选** | 工具不参与合并，方式随你 |
| 会话连续 | ✅ **resume 同会话** | 已实测（见 4.4），兜底 re-seed 保留 |
| 谁开 PR | ✅ **Orchestrator** | codex 只改码 + commit、工具建 PR：元数据可控、链路可靠 |

架构决策已全部锁定。

## 7. 头号风险 & 待核实

1. ~~codex CLI 非交互 / resume / 改码 / jj 提交~~ **已实测通过**（0.145.0，见 §4.4 / §4.6）：`codex exec` + `resume <id>` 上下文连续；`workspace-write` 下能改工作区，**jj `.jj` 可写、`jj commit` 成功**；**git `.git` 只读、`git commit` 被挡**。剩余待验：**git repo 提交路径**（这是 **sandbox 轴**的事——放宽 workspace-write 或换容器 Sandbox adapter，§4.6，非 VCS 层）、**push 联网 + scoped token**、多轮修订 diff/token 增长、并发多会话隔离。
2. codex 本机自动执行模型生成命令 → 限权 / 后续容器化。
3. webhook 本地投递 → 隧道或公网部署。
4. 并发写 base 冲突 → 每 flow 独立工作区 + push 前 rebase + 冲突转 FAILED。
5. webhook 长任务 vs 10s 超时 → 入队 + worker 异步跑 codex。

## 10. 待强化 / 已知短板

MVP 之前需要正视的短板。✅ 表示已回填进正文对应组件，⬜ 表示仍需定策略的开放项。

1. **持久化 job + 崩溃恢复** ✅ — 见 §4.2 持久化入队、§4.3 崩溃恢复、§4.7 `jobs` 表。
   codex 不再由 webhook task 直接拉起，`jobs` 表为真相，重启可 reconcile；副作用做幂等（防重复开 PR）。这也是 §9-6 `JobDispatch` 的种子。
2. **webhook 漏投 → reconcile 兜底** ✅ — 见 §4.2 对账兜底。webhook 主、周期轮询补漏。
3. **修订循环语义** ✅ — 见 **附录 B**（v3）：语义 Triage 门（`Judge` / bub）决定 revise/wait/answer/ignore；
   settle 窗口等会话停顿；行号漂移带 hunk + 原 sha；已处理线程自动 `resolveReviewThread`；并发合并排队、`/flow restart` 打断。
4. **触发授权 + 安全前移** ✅/⬜ — allowlist 与"issue 文本不可信"已入 §4.2 / §4.9；
   仍待定：**push 凭据** = Orchestrator 现签的**仓库级、短时、contents:write** token，经 git credential helper 注入、用完即弃、不落工作区（**App 私钥永不出 Orchestrator**）；**为 push 放开网络 egress**（由 **Sandbox 端口的 network 策略**控制，§4.6；尽量限到 github.com）。**打标 = 本机执行代码，安全是地基不是 M5。**
5. **Context Builder 一等组件** ✅ — 已单列 §4.9。
6. **状态机边界** ✅ — 见 §4.3 边界处理（PR 手动 close、外部 push、base 前进、CANCEL kill 进程）。
7. **成本 / 失控闸** ✅ — 见 §4.4：run 超时、最大修订轮数、预算上限、kill switch；resume context 累积 → re-seed / 压缩（待验）。
8. **上 webapp 前的可运维性** ⬜ — 补 admin CLI（重试失败 flow / 掐死 codex / 看日志）+ 结构化日志 + dry-run / mock 模式（录制 webhook fixtures + 假 codex）。

**优先级**：#1 / #3 / #4 最影响骨架、事后最难补，先做；#5 决定效果上限、边做边打磨；#8 在 webapp 之前补足。

