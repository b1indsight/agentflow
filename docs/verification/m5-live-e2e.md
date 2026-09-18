# M5 真实闭环验收

工具通过现有 gh 认证读写测试仓库，通过 control 环境变量连接数据库。
当前仅工具和自动化已实现，真实 App/Codex/Judge/Cloudflare 验收尚未执行。
部署准备见 [部署指南](../deployment/README.md)，fixture 见
[测试仓库任务](../../tests/fixtures/live_flow/README.md)。

## 操作范围与准备

准备专用空测试仓库、初始提交、App 安装/标签/allowlist，以及已运行的 serve/worker。
执行工具的 gh 登录身份必须是 allowlist 用户；模型认证只留在 worker。
固定应用版本，准备 control 的 `FLOWTOOL_DATABASE_URL`，不要把连接串放命令行参数。
工具不读取 `.env`，观察时通过安全的进程环境注入该变量，不要求其它 control secret。

所有阶段复用同一个 repo/run-id/私有 state-dir。state-dir 持久保留本地创建意图，禁止为了重试
删除它；只在同一部署主机使用该目录。多主机并发运行同一 run-id 不受本地文件锁保护。

先运行默认只读检查（替换仓库名和日期）：

```bash
uv run python -m app.live_smoke --repo OWNER/TEST-REPO --run-id m5-20260918 \
  --state-dir .cache/2026-09-18/live-smoke
```

确认是可写测试仓库后，显式创建一个带 flow 标签的 Issue：

```bash
uv run python -m app.live_smoke --repo OWNER/TEST-REPO --run-id m5-20260918 \
  --state-dir .cache/2026-09-18/live-smoke --allow-write --allowed-repo OWNER/TEST-REPO
```

只会创建 Issue，不自动评论、review、merge 或清理资源。创建前保存 unknown 意图；请求丢失
后只核对完整远端列表，不能从“暂时没看到”推断没创建。unknown 且查不到 Issue 时停止，
人工核查，不重建 state。REST 列表上限 1000 项，超过上限视为 incomplete，换干净测试仓库。
默认 trigger label 为 `flow`，该 fixture 不使用其它自定义 trigger label。

## 首轮与两轮 review

首轮 PR 交付后运行只读观察：

```bash
uv run python -m app.live_smoke --repo OWNER/TEST-REPO --run-id m5-20260918 \
  --state-dir .cache/2026-09-18/live-smoke --observe --source-revision ACTUAL_COMMIT \
  --wait-seconds 30
```

按 fixture 依次提出两轮人工反馈，各轮新提交完成后重新 observe。最后人工 merge，再 observe。
必须保存首轮及两次修订的三个不同已核验 head；首次观察就已合并的旧 PR 不补造历史证据。
source-revision 填实际部署的 commit/digest；工具在证据中同时记录协议版本和 UTC 时间。

默认不持续等待；`--wait-seconds` 上限 1800 秒，单次 gh 请求最多 30 秒且受剩余总预算限制。
退出 0：只读发现/显式创建操作成功，或 `--observe` 的核心条件全部满足；退出 2：观察尚未
满足条件；退出 1：错误/身份不匹配/结果未知。**普通检查的 0 不表示真实 E2E 通过。**
核心通过要求至少两轮持久修订、三个观察到的核验 head、真实 merged、归档结果提交和 binding
释放；这仅覆盖核心闭环，不自动宣告下面扩展矩阵完成。

## 扩展验收矩阵与记录

| 场景 | 实际操作/证据 | 当前状态 |
|---|---|---|
| App/Codex/Judge 预检 | 实际版本、配置存在性、worker 注册、首轮语义结果 | 未执行 |
| 入口与重复投递 | GitHub delivery ID、同 ID redelivery、唯一 Flow；无签名 401、私有路径 404 | 未执行 |
| 首轮与两轮修订 | Issue/PR、Flow/job IDs、三个 head、会话继续/fallback 事实 | 未执行 |
| answer/wait/ignore | 人工输入、持久批次 verdict、回复 ID；无不应发生的新提交 | 未执行 |
| 线程回复/resolve | 真实 thread ID、回复和远端 resolved 状态 | 未执行 |
| 人工合并与归档 | merge SHA、归档结果、标签/评论、清理凭据及 binding 释放 | 未执行 |
| serve 重启/worker 短断连 | 前后同一执行/结果身份，inbox/result replay 无重复副作用 | 未执行 |
| M4 对账/close/reopen | 丢事件补偿、暂停/恢复事实；独立测试场景 | 未执行 |
| Projects | 配置了真实 Project 时记录 item/status；未配置明确跳过 | 未执行 |

session fallback 在专属环境且有可恢复备份时构造，不能损坏未知真实会话强行验收。
取消/进程故障/备份恢复的完整演练留给 F6/F8。

run state 只写资源 ID、SHA、状态和时间，不写 token、原始 payload/prompt/模型输出。
完整报告还需手工补齐工具版本、delivery、thread、评论证据和未执行项，人工审阅后才提交摘要。
失败排查通过已有 `flow show`/`inbox status`；工具不直接修改业务表、删除数据或重试 Flow。
若创建了错误 Issue，人工取消/核对执行后清理；不要依赖当前 cancel 已经杀掉进程。

工具使用 [GitHub CLI API](https://cli.github.com/manual/gh_api) 的 JSON stdin 请求，不拼接 shell。
PR CI 只测试工具边界和数据库只读观测，不使用真实模型密钥或自动写测试仓库。
