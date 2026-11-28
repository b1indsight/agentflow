# Worker 写哪些本地文件？需要 SQLite 吗？

**建议目前保留文件，不给 worker 新增数据库。** 控制端 PostgreSQL 保存任务、分配和结果；执行 worker 需要在断线或重启后证明「哪个进程在这台机器上做过什么，哪个目录还属于这个 Flow」。本地记录必须和真实目录一起核对。最值得先考虑 SQLite 的是 `file-links.json`：它长期累积，即使只查一个 ID，也要解析整份注册表。[控制端与 worker 的完成边界](decisions.md) · [文件链接读写](../../app/worker/state/file_links.py#L22-L57)

## 哪些要跨重启保留

下表路径相对于 `AGENTFLOW_WORKER_ROOT`，默认 `~/.agentflow/workers/<worker_id>`。它列出 AgentFlow 定义的写入，以及它委托 CLI 写的目录；任意任务、依赖安装或原生 CLI 可以产生更多文件，文件名取决于实际运行的命令。[配置](../../app/worker/config.py#L24-L33) · [执行入口](../../app/worker/runtime.py#L502-L516)

| 路径 | 谁写、何时读、丢失会怎样 |
|---|---|
| `.worker-id`、`.worker.lock` | 执行 worker 首次记录 ID，以后启动时核对；进程持有锁文件上的 `flock`，防止两个执行进程共用 root。锁文件存在不表示进程还活着，运行中不要删除或替换它。[根目录锁](../../app/worker/runtime.py#L121-L159) |
| `workspace-pool.json` | 工作区管理器整份更新清单：root 路径、设备号和 inode，推理位置、各仓库槽位、Flow 绑定、分配代次、基线和清理凭据。启动、分配、复用和清理都要读；目录还在但清单丢失时拒绝接管。[结构](../../app/worker/execution/workspaces.py#L48-L98) · [校验与写入](../../app/worker/execution/workspaces.py#L140-L177) |
| `journals/flow-<flow_id>.json` | 每个 Flow 一份当前执行记录。接单、开跑、结果、停止和 ACK 分别落盘，重连可重报结果，不必再跑一次编码。普通结果 ACK 后仍可留档；成功归档并获 ACK 后才回收对应记录。旧 `assignment-journal.json` 和 `journals/current.json` 是启动迁移的输入，不是额外的现行写入点。[记录字段](../../app/worker/state/record.py#L35-L55) · [扫描、迁移与恢复](../../app/worker/state/execution.py#L62-L142) · [ACK 与回收](../../app/worker/state/execution.py#L418-L439) |
| `file-links.json` | 注册文件时保存不透明 ID、后端及来源：本地路径与设备号/inode，或 Amp 附件 URL。不复制正文；原文件被替换时不得把旧链接指向新文件。注册表跨任务保留，丢失后控制端的链接不能单独找回来源。[注册与查询](../../app/worker/state/file_links.py) · [本地身份校验](../../app/worker/coding_agent.py#L37-L60) · [控制端只存映射](../../app/persistence/file_links.py) |
| `amp-history/<session>.jsonl`、同名 `.json` | 使用 Amp 时，worker 将可展示的对话条目追加到 JSONL，JSON 检查点保存进度、条目 ID 锚点和脱敏替换内容。按会话、字节位置分页；记录不完整时可尝试原生 export 修复。不要把检查点当作可任意删除的缓存。[检查点](../../app/worker/execution/amp_history.py#L73-L96) · [追加与修复](../../app/worker/execution/amp_history.py#L121-L179) · [分页](../../app/worker/execution/amp_history.py#L258-L286) |
| `repos/<owner>/<repo>/`、`workspaces/<owner>/<repo>/<slot>/` | Worker 用 Git 刷新种子，按基线准备完整副本或 Git worktree；编码 agent 在工作区写项目文件、Git/jj 元数据、构建产物和缓存。完整副本可复用，额外的 worktree 在清理时注销并移除。配置 bot 身份还会写 Git 配置、`config.worktree` 和 `flowtool-hooks/`。[仓库准备](../../app/worker/execution/workspaces.py#L371-L412) · [清理](../../app/worker/execution/workspaces.py#L477-L517) · [Git 身份与 hooks](../../app/worker/execution/git_identity.py#L75-L115) |
| `inference/<slot>/` | 执行 worker 初始化并登记独立推理目录。配置 skills 来源后，在这里写 `skill-repositories/<sha>/` 快照和 `skill-versions.json` 版本索引；清理旧版本时保留仍有引用的快照。[推理目录](../../app/worker/execution/workspaces.py#L183-L218) · [版本发布](../../app/worker/execution/skills.py#L126-L151) · [引用检查](../../app/worker/execution/skills.py#L185-L219) |
| `skill-links-<slot>.json`、工作区内 `.agents/skills/<name>` 和 `.agents/skills/.gitignore` | 安装选中的 skills 时，worker 写根目录回执、工作区软链接及忽略规则。回执记录链接的目标；复用时据此只移除仍匹配的链接，保留项目自有文件。[回执路径](../../app/worker/execution/workspaces.py#L308-L314) · [清理和安装](../../app/worker/execution/skills.py#L249-L305) |

**备份边界大于 worker root。** Codex CLI 在 `CODEX_HOME`（默认 `~/.codex`）管理 `sessions/`、`archived_sessions/` 下的 `rollout-*.jsonl`，worker 只读；Amp 的原生登录也由 CLI 管理。保留专用 HOME、worker root 和控制端数据。把文件复制到另一块盘并不会复制原 root 或工作区的设备号和 inode；恢复时仍须通过身份校验。[Codex 历史读取](../../app/worker/execution/codex.py#L319-L359) · [部署与恢复](../deployment/docker.md#L120-L141)

## 哪些只在探活、调用或更新时写

| 位置 | 用途及生命周期 |
|---|---|
| `AGENTFLOW_WORKER_STATUS_PATH`（可选） | 每 5 秒写 PID、连接状态和时间，先写同目录 `.tmp` 后替换，退出时标记断开。路径由部署指定；不能据此释放工作区。进程日志默认到标准输出，没有对应的 worker 日志库。[心跳](../../app/worker/runtime.py#L568-L576) · [探针](../../app/worker/health.py#L12-L32) · [日志输出](../../app/telemetry/logging.py#L24-L30) |
| 编码/推理调用目录、系统临时目录 | `coding-schema-*.json` 或调用目录内的 `output-schema-*.json`；单次 Codex/Amp 目录内的任务附图、私有 `bin/gh` 与 `config/`；Codex 另有 `last-message.txt`。GitHub token 通过环境注入，不写进包装脚本或仓库配置。`execute=False` 推理另用临时 cwd，Bub 请求在临时 `BUB_HOME` 运行，不建编码 journal。[schema](../../app/worker/execution/executor.py#L434-L440) · [推理调用](../../app/worker/runtime.py#L305-L319) · [推理 schema](../../app/worker/coding_agent.py#L130-L144) · [Codex](../../app/worker/execution/codex.py#L196-L210) · [Amp](../../app/worker/execution/amp_agent.py#L439-L453) · [gh 环境](../../app/worker/execution/gh.py#L28-L53) · [Bub](../../app/worker/bub/__init__.py#L94-L108) |
| 附件下载、原生历史修复和 VCS 操作的临时目录 | 读取 Amp 附件时才下载到 `<root>/.amp-attachment-*/attachment`；历史回填在 `amp-history/` 下写候选文件。clone 在种子同级暂存，diff、修订 fetch、远端核验另用临时仓库或索引。临时数据正常退出时清理或发布，强杀可能留下残片，不能据此判断任务成功。[附件](../../app/worker/execution/amp_agent.py#L510-L536) · [历史发布](../../app/worker/execution/amp_history.py#L239-L255) · [clone](../../app/worker/execution/vcs.py#L177-L198) · [diff](../../app/worker/execution/workspace_diff.py#L25-L39) |
| worker 安装环境 | 显式 `worker --update` 会通过 uv 更新现有虚拟环境内的软件包和入口，不是每次接单的持久化步骤。[更新流程](../deployment/worker-update.md) |

## 什么情况下值得改用 SQLite

**当前的恢复单位通常是一个 Flow 或一个已登记的槽位。** 一个执行进程独占 root，但可并发处理不同 Flow。[私有记录写入](../../app/worker/state/files.py#L17-L33)用同目录 `0600` 临时文件、文件 `fsync`、原子替换和目录 `fsync`，保证单个记录发布完整；多个文件和 Git 操作之间没有共同事务。例如归档先持久化清理意图，再移除 worktree；若此时掉电，重启仍需比对目录和 Git 的登记信息。SQLite 也不能回滚一次 `git worktree remove`。[清理协议](../../app/worker/execution/workspaces.py#L477-L517) · [崩溃窗口测试](../../tests/test_workspace_archive.py#L157-L193)

**先量文件链接，再决定是否迁移。** [`FileLinks.lookup/register`](../../app/worker/state/file_links.py#L26-L57) 在锁内读取整个 JSON，注册还要扫描重复来源，新增时重写整表。本机用合成路径分别预置 100、1,000、10,000 条，每组交替执行 11 次单 ID 查询和新增；查询中位数约 0.06、0.39、4.53 ms，新增约 0.29、0.83、7.52 ms。这说明成本随表增长，不代表线上延迟。应看真实 worker 的注册表大小、调用量和高分位延迟；若已拖慢历史页或文件读取，优先只将链接元数据迁到带 ID 与来源索引的 SQLite 表，沿用旧 ID、来源去重和 inode 校验，正文留在原处。

若以后启动扫描 journal 明显超出恢复时限，或必须把**多条本地元数据**作为一次事务提交，再评估将清单和 journal 一起迁移。届时要验证旧记录导入、断电重放、ACK 和槽位代次；仓库、会话正文、skills 快照和临时文件继续留在文件系统。控制端仍负责跨 worker 调度与业务结果，不能靠本机数据库授权重跑或释放目录。[启动扫描](../../app/worker/runtime.py#L213-L224) · [丢失 journal 时的恢复测试](../../tests/test_workspace_archive.py#L194-L213)
