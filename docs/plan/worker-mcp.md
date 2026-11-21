# Worker 内 MCP 服务

**Stage:** Implemented

## 1. 方案和批准范围

用户要求将 MCP 集成进 worker，Codex 启动时注入工具，暂时仅保留 open_pr；明确确认
“worker 内提供 MCP 服务，移除独立桥接进程”。按已确认的方向实施，基于已合并的 PR #63。

当前 Codex 通过 STDIO 子进程和 Unix socket 转发调用。改为 WorkerMCP 在 worker 进程中
提供 loopback Streamable HTTP 服务，Codex 通过 URL 和任务专用 Bearer 环境变量连接。
复用 Starlette/Uvicorn 现有依赖，无新 MCP SDK/独立进程。只实现现有 open_pr 所需的
initialize、ping、tools/list、tools/call，以及通知接受；POST 返回 JSON，GET 不提供 SSE。

沿用 executor 的 assignment 生命周期：首次编码前启动，结束/取消时关闭。不新增常驻端口、
control API、WebSocket 消息或部署服务。监听 127.0.0.1 随机端口，校验 Host/Origin 和临时 Bearer，
凭据仅放入 Codex 子进程环境，不写参数、日志或 journal。服务不接管 worker 的信号处理。

open_pr 继续校验已推送分支，经 WorkerClient 转发 control，保存交付凭据供 executor 核对。
修订仍关闭该工具，启动/恢复时覆盖旧 flow MCP 配置，避免继续连接上一轮服务。
删除 tools.py 的 STDIO main、Unix socket bridge；保留 FlowToolCaller 协议和交付行为。
CodingExecutionContext 使用 mcp_url/mcp_token 替换 mcp_socket，更新 Codex 配置注入。

### 取舍与风险

现有子进程桥接不满足用户要求；单纯改名不能改变进程边界。
HTTP 使用已有服务器处理网络传输，避免自行解析 HTTP；有限 MCP 消息继续沿用项目的 JSON-RPC 处理。
loopback 替代 Unix socket 权限边界，必须保留 Bearer 和 Origin/Host 校验。
任务结束必须停止服务和在途调用；不能抢占 Runtime 的 SIGTERM 处理。
Codex 支持证据：官方 MCP 文档 https://learn.chatgpt.com/docs/extend/mcp?surface=cli；
本地 codex-cli 0.161.0 的 mcp list 已确认 URL/Bearer 配置为 streamable_http。
传输规范：https://modelcontextprotocol.io/specification/2025-06-18/basic/transports。

### 实施和验证

先把旧工具测试改为 HTTP 调用，再替换服务和注入；复用 executor/control 工具测试。
验证初始化、工具列表与调用、拒绝错误凭据/Origin、任务结束端口关闭、注入不泄露凭据，
停止/修订行为不变。全量回归、Ruff、Mypy、构建以及独立审查。
对新增鉴权做一次去除验证：相同未授权请求必须被正确实现拒绝；移除后测试应失败，恢复后通过。
不做旧 STDIO 兼容层或额外工具，不运行真实模型修改仓库。

## 2. 变更记录

### 1. 2026-10-08 — 内嵌 HTTP MCP 完成

WorkerMCP 取代 FlowToolBroker 与 STDIO main，使用同进程 Uvicorn/Starlette 服务，
只绑定本机随机端口；任务结束停止服务，覆写 capture_signals 保持 Runtime 的信号所有权。
Codex 注入整个 flow MCP 配置表，避免残留 STDIO command；Bearer 使用临时环境变量。
首次编码注入 open_pr，修订继续关闭；driver 对最终消息和 stderr 中的 MCP token 脱敏。
open_pr 仍经推送校验和现有 WorkerClient/control 处理，未改 worker/control 协议。

原 STDIO 工具测试替换为 HTTP 握手/列表/调用/拒绝/关闭测试，既有 executor 用例改走 HTTP。
另增加取消时信号/端口清理和凭据脱敏两项实际边界回归；无旧桥接兼容测试。
完整 PostgreSQL 回归 **828 passed**（56.90s，1 条既有 Starlette 弃用警告）；
Ruff、Mypy（156 文件）及 sdist/wheel 构建通过。
日志：`.cache/2026-10-08/worker-mcp-full.log`。

官方 MCP Python SDK 临时环境验证初始化协商到 2025-06-18、只发现 open_pr、ping 成功；
脚本 `.cache/2026-10-08/worker-mcp-smoke.py`，未添加 SDK 到项目依赖。
本地 codex-cli 0.161.0 的 mcp list 识别 URL/Bearer 注入为 streamable_http。
未运行真实模型或创建真实 GitHub PR 来验收业务交付；该路径由原 executor/control 回归覆盖。

消融 `.cache/2026-10-08/worker-mcp-ablation.py` 单独移除 Bearer 检查：同一未授权请求得到 200，
相应用例按预期失败；恢复源码后 HTTP/取消相关 2 项通过。日志 `.cache/2026-10-08/worker-mcp-ablation.log`。
保留 Bearer 作为替代原 Unix socket 文件权限的访问控制；Origin/Host 校验按 HTTP MCP 规范执行。
相对主分支，app 四个文件 +144/-181（净减 37 行）；无新运行依赖、常驻监听配置或额外进程。

### 2. 2026-10-08 — 处理 MCP 关闭后的迟到回复（用户明确要求修复）

独立审查指出 HTTP handler 关闭会取消工具等待 Future，而 control 的迟到回复仍写入它，
触发 InvalidStateError 并打断结果交付。用户已明确授权修复此问题。
新增真实 WorkerMCP/WorkerClient 回归：调用已发往 control 时关闭 MCP，随后返回成功或错误回复。
修复前两个场景均复现 InvalidStateError（`.cache/2026-10-08/mcp-cancel-before.log`）。
匹配并消费回复后检查 Future 是否已完成；已完成则继续协议流程，不再 set_result/set_exception。
不新增重试、屏蔽取消或延长服务生命周期，任务结果继续沿原连接交付并确认。

验证：830 项完整 PostgreSQL 回归通过（56.87s，1 条既有 Starlette 弃用警告），Ruff/Mypy 通过；日志 `.cache/2026-10-08/mcp-cancel-full.log`。新增判断的必要性由相同回归的修复前失败/修复后通过验证，不增加其他机制。

### 3. Worker MCP 搜索当前仓库的任务（拟实施）

首次编码的 Agent 可以通过现有 Worker MCP 调用只读工具 `search_flows`。例如 `{"q":"status:blocked 登录"}` 返回当前任务所属仓库里匹配的看板任务。仓库由 Control 根据已认证的 assignment 确定，不接受调用方指定搜索范围。修订仍不启动 MCP，不向外部客户端开放接口。

- **接口：** 必填字符串 `q` 使用看板语法；空串列出当前仓库任务。可选 `limit` 为 1–50（默认 50），`cursor` 取上页的 `next_cursor`。返回 `items`（Flow ID、标题、阶段、仓库、Issue/PR 编号）、分阶段 `counts`、`next_cursor` 和 `has_more`。`repo:` 仍是普通筛选，与授权范围取交集；指定其他仓库得到空页和零计数。非法查询或不匹配的游标返回错误。
- **实现顺序：** 先让 `ConsoleStore.flows` 接受独立于 `q` 的可信仓库条件，用于行查询、计数、分页上界和游标作用域；保持 Web 的无范围调用不变。再在已有 Worker 连接上增加只读请求/结果，每次校验当前连接、有效的首次编码 assignment、所属 Flow 和停止状态。最后注册 MCP 工具。不把数据库或 Web 控制台凭据交给 Worker，不复用 `open_pr` 的推送检查、交付锁、持久化回执或重放队列。
- **取舍：** Worker 侧过滤或拼接用户查询都不能充当服务端授权；直接使用看板查询可避免两份语法漂移。游标仅用于定位，每页都重新施加授权条件。不新增服务、依赖、数据库表或索引。
- **验收：** 两仓包含同名、同阶段的任务，以小页翻到末尾；核对所有行、计数及分页上界只来自授权仓库。覆盖空查询、异仓 `repo:`、跨仓游标、无效 assignment、停止状态。通过真实 MCP `tools/list` / `tools/call` 检查搜索可重复且不影响随后交付，并回归 Web 搜索及修订阶段行为。临时去掉仓库谓词时，双仓用例应失败。
