# 路线图

## 8. 当前目录与里程碑

```
flowtool/
  app/
    ingress.py        # FastAPI webhook 端点 + 验签
    router.py         # 事件归一 / 去重 / 路由
    engine.py         # Flow 状态机
    codex_driver.py   # CodexSession 抽象（CodeAgent 端口首个 adapter）
    context_builder.py # prompt / 上下文构建（§4.9）
    judge.py          # Judge / Triage 适配器（bub，§4.10）
    github_client.py  # githubkit 封装（App 认证）
    vcs.py            # workspace 隔离(VCS-无关) + VCS 指引文本(git / jj)，普通函数（§4.6）
    sandbox.py        # Sandbox 端口 adapter：codex workspace-write（§4.6 / §9-5）
    worker.py         # job 队列消费 + 崩溃恢复（§10-1）
    reconcile.py      # 对账 poller（§10-2）
    store.py          # SQLite 模型 + 仓储（含 jobs 表）
    config.py         # 配置（pydantic-settings）
    models.py         # 领域类型 / 枚举
    cli.py            # admin CLI（§10-8）
    plugins/          # 生命周期 hook 插件（§9-7）
  migrations/
  tests/
  pyproject.toml      # uv 管理
  README.md
```

- **M0（已验收）** 骨架 + webhook 验签 + SQLite（含 `jobs`）+ 配置 + worker / 崩溃恢复骨架；
  19 个自动化测试、Ruff、Mypy、CLI 与 `/healthz` runtime smoke 全部通过（2026-08-26）；
- **M1** 触发（含 allowlist）→ 工作区 → codex 首轮 → 开 PR（单 flow happy path）
- **M2** 语义 Triage（bub）门 + 回喂循环（settle → revise/answer/wait + resume/兜底 + 自动 resolve）
- **M3** 人工合并检测（`pull_request.closed`）→ 归档 + 状态标签 / 评论
- **M4** 多 flow 并发 + reconcile 对账 + Project 看板 + 错误恢复
- **M5** 安全隔离（容器化 codex）+ 公网 / 隧道部署 + 完整运维与可观测性

M1–M5 是目标里程碑，不代表对应控制流代码或接口已经验收；实际状态见[架构入口](README.md#当前实现状态)。

## 9. 未来扩展与架构预留（Roadmap）

> 以下是 MVP 之后的方向。列在这里是为了**现在就把接缝（ports）留好**，避免日后大改——
> core 只依赖抽象端口，扩展一律做成"换 adapter"或"挂插件"，不动 core。

1. **Web 控制台（Kanban）** —— 一个 webapp 打开项目看板，每张卡 = 一条被追踪的对话，可点进去实时参与对话。
   - 预留：core 状态经 store + 一层 API 暴露；headless（GitHub）与 webapp 是同一 core 的两个入口适配器。
     Driver 除"批量回喂 review"外，要留一个**实时注入消息**的入口，webapp 才能"插话"。

2. **多 agent 并行 / 多方案分支** —— 同一任务多个 agent 并行跑，或不同分支跑不同解法，再择优。
   - **数据模型接缝已就位（2026-08-02）**：引入 `attempts` 表，`issue ↔ PR` 不再 1:1 锁死——一条 flow 可挂多个 **attempt/candidate**
     （各自 branch / 工作区 / 会话 / PR）；`flows → attempts → runs` 一对多，`review_threads` 归属到具体 attempt，`flows.current_attempt_id` 指当前候选。
     待实现：真正并行起多个 attempt + 择优逻辑（engine 目前每 flow 只建一个 attempt）。

3. **完善 GitHub 协作** —— 覆盖常见协作动作（reviewer 指派、draft PR、labels、Projects 联动、review 线程 resolve 等）。
   - 预留：GitHub Client 保持薄封装，能力按需追加即可，不影响 core。

4. **Code Agent 标准接口** —— 把"与 code agent 交互"抽象成统一端口，方便接入新的底层 agent（不只 codex）。
   - **当前接缝已就位**：`CodeAgent` port 已定义 `start / resume`，Codex 是第一个 adapter；实时
     `inject`、统一日志/产物收集和其他 agent adapter 属于后续扩展。

5. **更强隔离** —— 每次 agent 运行放进容器 / 微VM，隔离文件系统与网络。
   - 预留：`Sandbox` port **已是一等轴（§4.6，M0 就立）**，MVP adapter = codex `workspace-write`；容器 / 微VM 是后续 adapter，对应 M5。
     那个 adapter 由它**接管整个工作区的 FS 策略**，git/jj 的差别塌成纯指引文本、顺带解掉 git `.git` 只读（原 V4）。

6. **控制面 / 执行面分离** —— webapp + webhook 入口与底层 code agent 可分在不同机器，从而**调度**被追踪的 job。
   - 预留：engine 触发 codex 的那一步走一个 `JobDispatch` port（MVP 进程内直接调；日后换成队列 + 远程 worker）。
     core 不假设 worker 在本机，job 状态经 store 共享。

7. **端口插件扩展** —— 在生命周期端口挂插件。
   例：合并后总结插件——归纳上一段对话里的代码风格偏好、更新仓库的 skill 文件。
   - **已接线（2026-08-02）**：五个生命周期 hook（`on_flow_start / on_pr_opened / on_review / on_merged / on_archived`）已在 engine 的对应状态转移处触发，插件可订阅；插件异常被隔离（不掀翻 core）。
     待实现：示例插件 `post-merge-summary` 的插件体（读会话 + diff → 提炼风格偏好 → 提 PR 更新 repo 的 skill），目前只打日志。

**架构取向：hexagonal（ports & adapters）。** core（Flow Engine + 状态机 + store）只依赖端口：
`CodeAgent` / `Judge` / `Sandbox` / `JobDispatch` / `GitHubClient` / `Plugin hooks`。
（**VCS 不是端口**——它是 workspace 拷贝函数 + git/jj 指引文本，见 §4.6。）
M0 已给端口放置最简 adapter 或桩（codex / bub / workspace-write / 进程内 / githubkit / 插件集）。
“存在 adapter”不代表外部契约已校准；未来扩展仍优先通过换 adapter 或加插件保持 core 边界稳定。
