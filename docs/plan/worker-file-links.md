# Worker 文件链接

## 目标与方案

文件 API 初版由 worker 显式登记可读文件，server 返回链接；读取链接时由 server 向原 worker 查询文件字节。API 初版不添加 WebUI、MCP 工具或 CLI，也不上传或缓存文件副本。

采用现有认证 WebSocket，登记与读取独立于 assignment。worker 只发送随机文件句柄；server 保存 `link_id → worker_id + file_id`，不接收本地路径。HTTP 沿用 Cloudflare Access 单 owner 鉴权，持有链接不代表获得权限。

API 初版实现顺序为协议与两端存储、连接消息分派、worker 登记方法、HTTP 查询，再验证端到端和错误边界；保留现有任务消息顺序、心跳、取消及恢复机制，未引入新依赖。

历史链接扩展在历史返回前仅改写发送副本的本地 Markdown 目的地址，控制台按实际文件名打开可切换、关闭和去重的详情页签，文本与代码显示原文件行号。当前实现以 006–010 为准；004–005 的早期提案保留作历史记录。原生历史、Amp 缓存和游标不改写，不新增 `file_refs`。文件在 512 KiB 上限内完整读取，仅挂载当前文件，不缓存文件内容，也不接入会话数据缓存；切回文件会重新读取，保留的页签和阅读位置不代表保存文件副本。详见[控制台读取语义](../../app/controler/web/README.md#页面与读取语义)。

## 使用

在已完成连接注册的 `WorkerRuntime` 所在事件循环调用；不要在 Session 的串行回调内等待此方法：

```python
from pathlib import Path

link = await runtime.register_file(Path("/path/to/report.txt"))
# /api/files/0123456789abcdef0123456789abcdef
```

- `WorkerRuntime.register_file(path: Path) -> str` 返回 **server-relative URL**；用 server 的 HTTP(S) origin 解析。连接未就绪或已断开时抛出 `FileFailure("worker_unavailable")`。
- 相对本地路径按 worker 进程当前目录解析；登记时解析符号链接，固定解析后的路径和设备/inode。同一身份重复登记或响应丢失后重试返回同一链接。
- `GET /api/files/{link_id}` 在 owner 鉴权成功后读取原 worker；成功返回原始字节，包含空文件及二进制文件。控制台使用 `fetch` 按需读取内容。
- 响应固定为 `application/octet-stream`、`Content-Disposition: attachment`，并带 `no-store`、`nosniff`、`no-referrer` 和 sandbox CSP。HTML/SVG 等内容不在服务端 origin 执行。
- 文件 API 返回的 URL、HTTP 元数据、文件 RPC 和错误不含 worker 路径或文件名；历史显示链接另加 `#name=<编码后的 basename>` 供页签和下载命名，片段不发送给文件端点，也不包含目录。**文件正文按原样返回**，若正文自身含路径或秘密不会自动脱敏。登记文件的调用者负责选择可公开给 owner 的内容。

## 存储与接口

- `app/worker/state/file_links.py`：`LocalFileLinks.register(path) -> str` 与 `read(handle) -> bytes`。`file-links.json` 位于 worker root，以 0600 原子写入，沿用 worker root 独占锁。每个随机句柄对应 `LocalFile(path: Path, device: int, inode: int)`。线程锁串行化注册表更新。
- `app/persistence/file_links.py`：`FileLinkStore.register(worker_id, file_id) -> FileLink` 以 `(worker_id, file_id)` 唯一键幂等登记；`get(link_id) -> FileLink | None` 查询路由。`FileLink` 只有 `link_id`、`worker_id`、`file_id` 三个字符串字段。数据库不保存路径、文件名、正文。
- `app/contracts/protocol.py`：`FileRegister`/`FileRegistered` 使用 `server.file_register`，`FileQuery`/`FileResult` 使用 `worker.file_read`，携带随机句柄、链接、base64 字节或固定错误。请求继承 `ToolCall`、回复继承 `ToolResult`，分别属于 `ServerCalls`/`ServerResults` 和 `WorkerCalls`/`WorkerResults` 静态包别名；方向联合和固定方法集合由这些别名派生。调用以 `message_id`/`reply_to` 关联，epoch 位于顶层信封，不另加文件消息信封或路径字段。
- `app/controler/workers/files.py`：`FileService` 从共享 Gateway 选择原 worker 的 Session，使用 `request_reply` 等待回复，不再自管连接或关联表。连接回调核验 epoch，Dispatcher 关联请求；未知、旧连接和其他 worker 的回复不能完成查询。
- `ServerFileCallbacks` 和 `WorkerFileCallbacks` 各自持有有界 IO 任务，完成后唤醒串行 driver 发送结果，不占用执行/推理的 watch 槽。慢登记或文件读取不阻塞执行消息；其他串行业务回调仍可能延迟文件回复。超时/取消后的迟到回复被忽略，不断开健康连接。

## 边界与错误

- 只读取普通可读文件，最大 **512 KiB**；base64 编码后的消息低于默认 1 MiB WebSocket 限制。读取实际最多 `limit + 1` 字节，不只信任 `stat`，防止文件增长越界。更大文件、Range、流式读取留待后续。
- 使用非阻塞打开并检查文件类型，FIFO/设备/目录不作为文件读取。读取时拒绝最终路径分量的符号链接和不同设备/inode。
- 链接跟随同 inode 的原地修改；原子替换文件需要重新登记。工作目录被复用后，不因同名的新 inode 自动改变旧链接的目标。它不是内容快照，也不保证阻止同 inode 被覆写或文件系统复用 inode。
- server 同时最多 64 个在途文件读取，每个 worker 最多 4 个；worker 最多同时处理 4 个读取和 4 个登记请求。登记与 worker 查询等待上限为 10 秒。取消 HTTP 请求、断开或替换连接会释放 server 在途槽位；已经开始的本地文件系统调用不能被 Python 线程取消。
- `404 file_not_found`：未知链接、worker 本地记录丢失或文件已删除。
- `409 file_changed`：文件身份已变；`413 file_too_large`：超过上限。
- `503 worker_unavailable/query_busy`：worker 离线或超出并发；`504 query_timeout`：worker 未及时回复。
- `502 file_unreadable/invalid_response`：文件无法读取或 worker 回复无效。数据库失败沿用 `503 database_unavailable`；登记存储失败返回 `registration_unavailable`。
- 登记映射随数据库及 worker root 保留，重连/同版本重启不需重新登记；清理了文件只会导致读取失败。此版不做过期、撤销、后台 GC、跨 worker 迁移或多 serve 路由。

## 协议与部署

当前协议以 main 的 [`PROTOCOL_VERSION`](../../app/contracts/protocol.py#L44) 为准，使用 **wire v26**，两端必须协调升级，旧 wire 不在线互通。保留 main 的 journal v5、执行 ResultInbox v1 / Control 通知 inbox v2 和 workspace manifest v3；文件服务使用既有 `worker_file_links` 表。main 的 worker/workspace 复合主键不允许文件表引用单独的 worker_id，因此文件路由保存已认证的逻辑 worker ID，不绑定可回收的 workspace 行。历史改写、文件名片段和行号预览不新增 wire 字段或持久格式；下文保留文件服务最初的 wire v17 及整合时 wire v25 的历史记录。既有数据库与 journal 的升级限制仍见[升级说明](../operations/upgrade.md)。

## 实施与验证记录

### 001 — 文件链接登记和按需读取

最初实现使用 `WorkerClient.register_file` 和 wire v17（journal v4、manifest v1）；下述 003 记录合并 main 后的接口与通信变更。

已实现上述接口、持久映射、身份校验、读取限额及 owner HTTP 路由。选择服务端返回相对链接以避免增加 public-origin 配置；选择两端保存映射以避免把路径交给 server。与直接让 HTTP 传入路径相比，worker 仅能返回已登记身份；与保存文件副本相比，查询得到 worker 当前内容，但需要 worker 在线。

已执行真实 PostgreSQL 下的 `tests/test_file_links.py`、`tests/test_file_proxy.py` 和 `tests/test_conversation_api.py`：覆盖空文件、二进制、大小边界、并发重复登记、忙碌与空闲 worker、两端对象重建、owner 鉴权、路径不出现在消息/响应元数据、替换/删除/不可读/FIFO、跨 worker 和旧 epoch 回复、超时、断线、并发限制、慢登记不阻塞执行消息、HTTP 断开清理。消息传输采用内存队列，HTTP 使用 ASGITransport；不将这些结果等同于真实公网部署验证。

全量 PostgreSQL pytest、Ruff、Mypy、`uv build --no-sources` 和 `flowtool --help` 均通过。前端 `npm run format:check`、`npm test` 和 `npm run build` 通过；Playwright 默认端口 5174 被现有进程占用，使用临时配置仅切换到 55178 后全套通过，未停止原进程或修改仓库测试配置。

### 002 — 真实传输和简化对照

- 使用本机临时 Uvicorn、真实 WebSocket 客户端及独立 PostgreSQL schema 验证登记和 HTTP 读取：524288 字节二进制内容逐字节一致，无 owner assertion 返回 401，响应无文件名。认证签名使用本机测试 issuer，未验证真实 Cloudflare、公网或部署环境。
- 在临时 Python 进程中替换 `file_links._read`，仅移除 `expected` 身份比较后运行 `test_links_reject_file_substitution_and_growth`，测试因替换文件被接受而失败；正常实现通过，因此保留身份检查。
- 临时替换 `FileService.accept`，将回复强制视为 pending 请求的连接/epoch 后运行 `test_file_routing_isolates_worker_identity_and_connection_epoch`。首次对照发现测试中伪造与合法回复均为空，不能识别误接收；改用不同内容后，对照返回 `b"forged"` 而失败，正常实现通过，因此保留连接与 epoch 检查。
- 所有对照仅修改该进程内对象，未改动产品源文件；最终针对性测试、Ruff、Mypy 再次通过。

### 003 — PR #146 合并 main 与调用位置核查

针对反馈 item 1/version 1/comment 5888832200，以保留原提交为第一父节点的 merge 方式整合 main。保留 main 的统一 Session/Gateway、并发执行、推理和 workspace 模型；删除 main 已淘汰的 WorkerClient、WorkerConnection、WorkerGateway，将文件登记入口移到 WorkerRuntime。只复用通信层的请求生命周期，不再保留旧协议连接表和回复分派。两端有界 IO 任务避免慢数据库/文件读取阻塞串行消息处理，迟到文件回复不会被当成执行协议错误。

针对 item 2/version 1/comment 4561730074，`ServerRuntime.files` 有实际生产调用：`app/controler/api.py` 的 `file_content` 路由执行 `runtime.files.read(link_id)`，即 `GET /api/files/{link_id}`。合并后的 runtime 直接构造 `FileService(self.gateway, database=database)`；`serve_worker_session` 还将其 `.store` 交给连接级登记回调。保留该属性，未将提问解释为删除要求。

文件集成测试已改用正常构造的 ServerRuntime/WorkerRuntime、真实 PostgreSQL 和共享 Session。覆盖 idle/busy 登记读取、对象重建、owner HTTP 路由、陈旧 epoch 注册拒绝，以及慢 IO 期间执行仍能完成、取消/超时后的迟到回复不破坏连接。

合并后的验证结果：

- 全量 PostgreSQL pytest 通过；Git 参数组合下不适用的 jj 元数据保留用例按现有条件跳过。最终文件/通信/协议/HTTP 专项测试、Ruff、Mypy、CLI help、前端格式/单测/构建和 sdist/wheel 构建通过，wheel 包含 console 与新增文件传输模块。
- 前端与 main 逐字节一致。Playwright 首次四 worker 并行运行时，`large activity stays windowed` 的滚动位置断言失败；降低到单 worker 后该用例及完整套件均通过。未改断言或 UI，具体时序原因未确认。
- 使用合并后代码重新执行本机 Uvicorn、真实 WebSocket、HTTP 与 PostgreSQL 验证，512 KiB 二进制逐字节一致，无 owner assertion 返回 401，链接和响应头无文件名。未执行公网部署验收。
- 临时进程将 server 登记改为直接等待 IO，慢登记期间执行确认超时；恢复当前实现后相同测试通过。另一个临时进程恢复“拒绝迟到 FileRegistered”的行为，取消后的登记回复触发 WorkerProtocolError；当前实现通过。这两个对照支持保留后台 IO 与迟到回复处理，未改产品文件，临时测试脚本已删除。

### 004 — 会话本地文件链接与新标签页预览（提案，待批准）

**目标：** 会话中指向 worker 本地文件的 Markdown 链接，显示时使用 worker 登记的文件链接；点击后在新标签页查看内容。当前只完成方案，尚未修改产品代码或测试，未提交、推送或部署。

#### 当前事实与基线

- 以已抓取的 `origin/main` 为设计基线；文件登记和读取已在 [PR #146 的合并提交](https://github.com/b1indsight/agentflow/commit/dc8e26e) 中提供。本地共享工作区尚未包含这些代码，且有其他未提交工作，实施时必须保留，不能直接重置或覆盖。
- [现有文件通道](#使用)已支持 worker 本地登记、持久随机句柄和 owner 鉴权读取，最多 512 KiB。HTTP 固定为下载，单独添加 `target="_blank"` 不能完成预览。
- [Markdown 渲染](../../app/controler/web/activity.tsx)目前过滤 `file:`，普通相对地址按消息来源解析。新增规则只作用于 worker 消息，不改变 GitHub 相对链接、脚注、工具原文或外部链接。
- [历史回调](../../app/worker/transport/business.py)在 Session 串行 driver 内执行，不能在其中等待 `register_file` 的网络回复。本提案在 worker 本地登记文件句柄，再由 server 复用现有登记仓储生成 URL，避免新增逐文件 WebSocket 往返。

#### 拟议体验与边界

- 识别 worker 来源、`kind=message` 的 user/assistant 正文中真实 Markdown 链接与图片的目标，支持 `file:///绝对路径` 和 POSIX 绝对路径；`file://localhost/…` 视为本机，其他 host、相对文件路径及普通裸路径不推断。图片仍显示为链接，不在会话内自动加载。
- 示例：`[查看报告](file:///work/report.txt)` 的标签不变，目标变为 `/console/#file/<link_id>`；预览页使用已登记的 `/api/files/<link_id>` 读取字节。链接继续使用 `target="_blank"` 和 `rel="noopener noreferrer"`，不改变当前详情页 hash 或阅读位置。
- 文本、Markdown、源码及 HTML/SVG 源码以转义文本显示；PNG/JPEG/GIF/WebP 按内容签名确认后显示为图片。其他二进制提供原下载入口。首版不执行 HTML、JavaScript 或 SVG，也不实现富文本 Markdown 文件预览。
- 单个文件登记失败不阻断会话正文。未登记的本地目标保留标签并注明暂不可预览，不生成猜测 URL；预览页区分鉴权失效、文件丢失/替换、超限、worker 离线、超时，并提供适用的重试入口。超过 512 KiB 的文件同一下载 API 也无法读取，页面须明确说明。
- 保留现有同 inode 当前内容语义，不宣称内容快照、离线可读或永久可用。旧归档正文不修改；已有消息通过显式刷新后的分批 worker 重读补齐映射，无需重跑编码任务。worker 离线或历史已丢失时仍显示旧正文，并说明链接尚未补齐。

#### 最小技术方案

1. **worker 本地登记。** `WorkerRuntime.read_history(query: HistoryQuery) -> HistoryPage` 保持同步接口，仍在线程中调用。读取原生历史后，用 CommonMark 解析器仅提取真实链接/图片目的地址，再调用现有 `LocalFileLinks.register(path: Path) -> str`。不改写原文，不新增 `register_many` 或另一套登记服务。
2. **页级句柄映射。** `HistoryPage` 新增默认空的 `file_refs: dict[str, dict[str, FileId]]`，外层键为本页 `HistoryItem.id`，内层为规范化链接目标到已登记句柄。新增字段在原生历史分页完成后填入，不进入 `HistoryItem`；尤其不改变 Amp 对原始 items 的摘要或 Codex 的文件游标。
3. **server 生成链接。** `ConversationService.read(flow_id: int, cursor: str | None, limit: int) -> ConversationPage` 校验映射只引用本页消息，使用已认证连接的 worker 身份及 `FileLinkStore.register(worker_id, file_id)`，为 `ConversationItem` 新增默认空的 `file_links: dict[str, str]`，值严格限定为 `/api/files/<32位小写十六进制ID>`。数据库操作在线程中执行，worker 从不把路径交给登记/读取 RPC；目的地址仅作为原有会话正文的显示索引，server 不据此访问文件系统。
4. **沿用归档。** `ConsoleSynchronizer._worker(item: ConversationItem, claim: SyncClaim) -> Observation` 将映射编码为 JSON 字符串放入 `metadata.file_links`，复用现有标量 metadata 与版本归档，不拓宽所有来源的 Observation 合同。前端边界解码并校验，不能直接信任任意 metadata URL。
5. **显示时替换。** `ActivityMarkdown({text, url, fileLinks?})` 只对合法映射命中的目标生成预览路由，再对未命中目标沿用默认 URL 过滤。worker 身份由 Activity 的 source 确定；GitHub 内容不能通过伪造本地路径获得登记权限。链接目标规范化在 worker/前端遵守同一合同，覆盖中文、空格、百分号转义和片段。
6. **独立预览页。** 新增 `FileViewer({linkId: string})`，在现有 React 入口识别严格的 `#file/<id>`，不挂载看板或触发 Flow 列表请求。`getFile(linkId: string, signal: AbortSignal) -> Promise<Uint8Array>` 同源、有界读取原接口；超限立即中止。文本复用既有分段规则，图片限制容器宽度，图片资源卸载后释放。预览所需的 Blob/data URL 权限只作必要调整；不改变下载 API 的 MIME、attachment 或 sandbox 响应头。

**数量和时间预算：** 每页最多登记 16 个唯一目标，同一目标复用句柄；单目标最多 1024 字符，映射和响应一起计入既有页字节上限。超出预算的目标明确标注为未生成预览，不能暗示文件不存在。保持历史请求的既有超时，不对单文件做无限重试。

**自动登记的授权含义：** 本提案将 worker 的 user/assistant 消息中的显式本地文件链接视为向当前 owner 提供该文件的意图，允许 `.amp/in/artifacts/` 等产物路径；不因目录以点开头就全部拒绝。普通文字、代码、工具参数和 system/developer 指令里的路径不触发登记。文件只向已通过现有 Access 校验的 owner 提供，正文仍不脱敏；浏览器不能提交任意 worker 路径来申请读取。

**解析依赖：** 使用真正的 CommonMark token 解析，避免用路径正则扫描整篇正文。`markdown-it-py` 已在当前锁文件中，若选用则声明直接依赖；仅为链接提取启用所需本地目的地址识别，不放宽前端的危险协议过滤。无需引入前端 Markdown 框架或新文件基础设施。

#### 方案取舍与成本

- 选择页级映射，保留原文和历史身份；直接改写正文会混入文件可用性与读取时机，影响归档差异。映射额外占用有界协议和归档空间。
- 选择 worker 本地登记、server 复用仓储，避免在串行回调等待回复及每文件额外网络往返。现有 `register_file` 继续作为其他调用者的完整 API，不为此重构通信框架。
- 保留下载端点并加前端查看页，避免把未知 HTML/SVG 作为同源可执行文档返回。代价是首版 HTML 只能看源码，其他二进制只能下载。
- 新映射字段需要按实施时最新 main 分配 wire 版本，协调升级 server/worker。不新增数据库表或文件传输方式；不硬编码当前猜测的下一版号。
- 旧 worker 来源游标需要一次投影版本失效，复用现有失效后分批重读和幂等归档机制。显式刷新也必须能重试未成功的链接；不在一次详情 HTTP 请求内遍历全部历史，不添加周期全库扫描。

#### 实施顺序与验证标准

1. 安全整合上游既有文件能力，保留当前共享工作区的其他改动；再实现页级引用合同、worker 提取和本地登记、server 映射与归档，递增 wire 版本。
2. 接入旧消息的有界重读与失败重试，随后实现显示时替换和独立预览页；更新运行文档，不改变当前会话布局。
3. 执行以下验证后再进行代码审查；所有项目目前均为计划，尚未运行：
   - 真实 Session + PostgreSQL 验证 Codex/Amp 历史中的本地链接，经登记、会话同步、详情点击到新标签内容逐字节可达；覆盖已有归档刷新、同名不同文件、当前工作区复用与文件替换。
   - 对照输入包括普通外链/GitHub 相对链接、脚注、代码块和行内代码、工具参数、无权限或不存在文件、图片、中文/空格/编码路径；检查仅真实本地链接登记，原正文、历史 items、cursor/digest、读取预算和 owner 鉴权保持原合同。
   - 覆盖空文件、UTF-8、多字节边界、HTML/SVG 不执行、伪装图片、非 UTF-8 二进制、512 KiB 边界和超限、离线/断线/登记失败；失败不能丢失正文或误提示下载可用。
   - 浏览器实际点击及键盘激活，检查新标签 URL、原详情路由/阅读位置、预览刷新直达、无多余看板请求；桌面与手机检查文本、图片、错误和下载状态，捕获并查看截图。
   - 执行改动范围的 Python/前端测试、格式/类型检查、构建与打包。保留现有下载和文件身份测试。
4. 简化对照：临时移除页级映射时，本地目标仍不可打开且其他链接回归应通过；临时关闭预览隔离时，恶意 HTML/SVG 的不执行验收必须失败。对照只用于验证，不保留临时修改或脚本。

#### 待批准与后续边界

审批内容为上述自动登记范围、复用现有文件通道、受保护的新标签页预览，以及 HTML/SVG 首版显示源码。相对文件路径、裸路径自动识别、可执行 HTML、外链资源、超大文件、Range、离线快照、文件浏览器与新 MCP/CLI 均不在本次范围。

实施时再核对 CommonMark 与 remark 的目标编码一致性、同步游标失效与重试的具体接入点；若证据要求改变上述合同，先记录修订。方案起草使用了本机 Claude Code 的 `claude-opus-5-5`，已核实工具和 MCP 均为空；本文件中的方案已经按实际代码复核和收敛，模型建议不作为运行验证结果。

### 005 — 历史返回前替换链接，在 Flow 详情 tab 查看（修订方案，待批准）

**需求更新：** 用户要求在返回历史时先过滤内容、收集本地链接、登记文件、直接替换为 controller 文件链接；点击后在详情 tab 展示。此修订取代 004 的页级映射、server 补全映射、前端显示时映射和独立浏览器标签页方案。004 保留为此前提案记录，产品与测试均尚未实施。

#### 简化后的流程

1. controller 沿用现有 `worker.history` 按需查询，worker 读取原始历史页并计算原生游标；不另建历史上传流。
2. worker 复制待发送页，解析正文中真正的本地 Markdown 链接/图片目标，跳过代码、普通裸路径、外链和脚注；同一文件在本页内去重。沿用 004 的绝对本地路径范围，不把相对路径猜成 worker 进程目录。
3. worker 复用现有 `await register_file(path)`，取得 `/api/files/<id>`；仅在副本中替换链接目的地址，再返回原有 `HistoryPage`。保持链接标签、title、引用名和非目标文字；引用式链接修改定义目标，不能对全文作路径字符串替换。
4. controller 按现有同步、归档和正文分页逻辑保存并展示替换后的文本。无需 `HistoryPage.file_refs`、`ConversationItem.file_links`、`metadata.file_links` 或原路径到 URL 的持久显示映射。
5. 用户在会话中点击文件链接时，在当前 Flow 详情区域切换到一个可复用的「文件」tab。后续点击其他文件更新同一 tab；切回「会话」恢复已有阅读位置，不打开新浏览器标签。普通外部链接继续沿用现有打开方式。

#### 仅修改现有职责和必要查看组件

- `WorkerRuntime.read_history(query: HistoryQuery) -> HistoryPage` 改为异步方法：在线程中读取原生历史，随后复制页、收集目标、登记和替换。不保留同步方法外再包一层同职责入口。
- `WorkerBusinessCallbacks.on_history` 只接纳有界后台任务并立即返回，沿用现有文件回调的任务完成后唤醒 driver、在 `prepare` 发送结果、在 `aclose` 取消和回收的模式。不能在 Session 串行 driver 内直接等待登记回复，也不添加新的通信框架。
- `ActivityMarkdown` 对 worker 活动中精确匹配 `/api/files/[0-9a-f]{32}` 的同源链接提供详情内打开动作，在现有相对 URL 解析之前识别；前端不再接收或查询原路径映射。GitHub 相对链接和跨域同路径 URL 不能被误当作本地文件入口。
- `App.tsx` 复用现有详情 Tabs，增加单个「文件」tab 并保存当前文件 ID；`FileViewer({linkId: string})` 在详情正文区域同源读取现有文件 API。复用 004 的文本/安全位图/下载范围、错误状态、大小上限和鉴权；移除独立 `#file/<id>` 页面。首版不增加多文件 tab 管理、全局文件列表或新窗口。

#### 必须保留的边界

- 原始 Codex/Amp 历史和 Amp 缓存不改写。Amp 页中的条目直接引用缓存对象，必须对发送副本替换正文，不能让修改进入后续摘要；原生游标仍由替换前的原始 items 计算。controller 保存的正文明确为处理后的显示内容，不再声称它与 worker 原文逐字相同。
- 登记有数量、并发及剩余请求时间预算，兼容现有 30 秒历史查询和 10 秒单次登记上限。登记失败、忙碌或超时时保留该目标原文，其余已成功替换仍可返回；不因等待登记丢失整页正文。断线关闭时回收关联任务，不向替换后的连接发送旧页结果。
- 替换后重新验证每条 8192 字符、来源页 256 KiB 及控制端 512 KiB 响应限制；若某次替换会超限，跳过该目标，不能截断 Markdown 或静默丢消息。不按处理后的文本重算原生历史 cursor。
- 不新增历史 wire 字段或协议对象；此转换本身不再要求为映射字段提升 wire 版本。现有文件服务所需的上游版本仍须正常整合，不覆盖共享工作区其他改动。旧消息刷新和登记重试沿用 004 的分批重读目标，但归档对象变为处理后的正文。
- 自动登记仍只由实际 worker 消息的本地链接触发，前端不能提供任意本地路径申请读取。原生历史保留原文；URL 与文件读取 RPC 继续仅使用随机句柄，读取仍要求 owner 身份。

#### 验证与当前状态

实施后需验证：重复查询的稳定链接及原生 cursor、Amp 缓存不变；慢/失败登记期间正文可返回且 driver 继续处理消息；仅目的地址改变，代码/脚注/外链/引用式链接及中文/空格/百分号路径正确；替换后长度边界不丢记录；真实 Session + PostgreSQL 同步后的正文已含 controller URL；点击与键盘激活在同一 Flow 的文件 tab 显示，切回会话恢复阅读位置，桌面/手机及错误状态无回归。此前 004 中的映射字段和新浏览器标签页验收由这些检查替代。

**当前交付：** 已按用户反馈更新方案并核对现有调用、缓存和 Session 边界；尚未修改功能代码、运行功能测试、提交、推送或部署。修订草案经本机 Opus 5.5 无工具咨询生成后复核；模型建议不是执行验证。待确认本修订后实施。

### 006 — 返回前改写正文与详情内文件预览

按用户确认的 005 实施。安全整合 `origin/main` 已有的文件服务及 wire25 接线，保留共享工作区原有修改；本次正文转换没有增加历史 wire 字段、`file_refs`、归档 metadata 映射或任意路径读取 API。

**Worker 处理。** `WorkerRuntime.read_history` 直接改为异步：在线程中读取原生页，再交给 `state/history_links.py` 复制、提取、登记、替换。CommonMark 规则仅收集 user/assistant 消息中实际链接和图片的目的地址，引用式链接只修改有效定义；保留标签、title、引用名、CRLF 和其他文字。代码、工具/system/developer 内容、脚注、未使用定义、外链和相对路径不登记。中文、空格、百分号、转义括号、列表/引用前缀与 Unicode 分隔符的偏移均经回归验证。声明现有锁文件中 `markdown-it-py` 和 `mdit-py-plugins` 为直接依赖，避免自行实现 Markdown 语法或全文字符串替换。

**预算和连接生命周期。** 每页按源顺序最多登记 16 个唯一目标，目标最多 1024 字符，并发最多 4。原生读取与登记共享 25 秒总预算，为 controller 的 30 秒等待保留回复时间；单次登记沿用文件服务的 10 秒上限。失败或截止保留原目标及已经成功的改写，超出单条 8192 字符、来源页 256 KiB 或响应 512 KiB 的替换逐个跳过，不截断消息。`WorkerBusinessCallbacks` 每连接最多接纳两个后台历史任务，保留请求 epoch，完成后唤醒 driver 发送，关闭时取消并等待回收；注册握手期间的既有历史读取仍可用。

**缓存、旧历史和重试。** 深复制发送页，Amp 缓存及原生 cursor/digest 保持不变。`ConversationService` 的外层来源指纹加入处理版本，通过已有 cursor-invalid 恢复做一次有界重读。显式刷新在既有发布锁顺序内清除 worker checkpoint 并撤销旧 claim，避免迟到 EOF 覆盖刷新；随后仍逐页同步，支持已归档 Flow 及登记失败后重试。自动同步继续增量读取，刷新不创建或重跑编码任务。

**详情查看。** `App.tsx` 复用当前 Tabs，增加单个“文件”tab。`activity.tsx` 仅在 worker 来源里、普通相对 URL 解析前识别精确同源文件链接，通过现有详情上下文切换并转移键盘焦点；返回会话恢复逻辑阅读位置。`FileViewer.tsx` 与 `files.ts` 消费原 attachment API，按实际响应流限制 512 KiB，切换或重试时取消并隔离旧结果。UTF-8/Markdown/HTML/SVG 通过既有 `TextWindow` 显示转义源码；PNG/JPEG/GIF/WebP 须通过签名及浏览器实际解码，使用既有 CSP 允许的 data 图片，不放宽 CSP。其他二进制提供原下载入口；超限及读取失败不误示下载可用。当前文件 ID 仅留在当前详情内存，页面刷新后从会话重新选择。

**执行验证。** 已执行真实 PostgreSQL 下的全量 Python 测试、Ruff、应用与测试的 Mypy、sdist/wheel 构建、wheel 内容核查与 CLI help。专项覆盖真实双端 Session、旧历史两页归档、先缺失后刷新补链及逐字节读取、原生缓存与 cursor、部分失败/超时、长度预算、并发上限、断线回收及重连。消息传输使用内存队列；没有将其当作公网 WebSocket 或部署验证。前端单测、类型、格式和生产构建通过，完整浏览器回归及键盘 Tabs 重复执行通过；实测点击、Enter、单 tab 复用、阅读位置、脚注/外链控制、安全源码、四种实际位图解码、空文件、伪装图片、二进制、离线重试、超限/鉴权与迟到响应。浏览器使用 API 拦截，没有验证生产 Access 或线上 Worker。生产构建仍有非阻断的 chunk 大小提示。

复现命令：在仓库根执行 `FLOWTOOL_TEST_DATABASE_URL=<本机测试库> uv run pytest -q`、`uv run ruff check app tests`、`uv run mypy app tests`；在 `app/controler/web` 执行 `npm test`、`npm run typecheck`、`npm run format:check`、`npm run build`，以及 `CI=1 PLAYWRIGHT_PORT=<空闲端口> npm run test:e2e -- --workers=1`。桌面和手机的源码、图片、二进制和离线截图已实际查看，位置为 `.amp/in/artifacts/worker-file-{text,image,binary,error}-{1440,390}.png`；无页面或详情区横向溢出由浏览器断言验证，脚本不执行由 DOM/运行状态断言验证。

打包另按既有文档运行 `uv run python scripts/build_console.py` 后再执行 `uv build --no-sources`；仅运行 `npm run build` 不会复制资源至 Python 包目录。最终 wheel 已核查包含新的 `file-viewer`、`/api/files/` 前端代码及 `history_links.py`。测试、浏览器、Mypy、构建及简化对照日志均保存在 `.amp/in/artifacts/worker-file-links-*.log`。

### 007 — 必要机制的进程内简化对照

仅在独立 `uv run python` 进程内用 `inspect.getsource` 和 `exec` 替换选定函数，再通过 `pytest.main` 执行相同验收用例；没有改写产品文件。对照日志在 `.amp/in/artifacts/worker-file-links-ablation.log`。

- **移除发送副本。** 从 `link_history` 中移除 `page = page.model_copy(deep=True)`，运行 `tests/test_history_file_links.py::test_amp_repeat_and_next_page_keep_native_cursor_and_cache`。Amp 原始查询对象的正文变成 controller URL，原文断言失败；保留深复制。
- **移除响应尺寸检查。** 只将 `link_history` 的页面/响应尺寸条件替换为 `if False`，保留单条上限，运行 `tests/test_history_file_links.py::test_expanding_short_links_never_overflows_item_or_page`。短目标膨胀令来源页超过 256 KiB，测试失败；保留逐次检查和跳过替换。
- **改回 driver 内等待。** 将 `WorkerBusinessCallbacks.on_history` 临时替换为直接等待 `_read_history` 后发送，运行 `tests/test_conversation_worker.py::test_history_does_not_block_separate_registration`。握手尚未完成的场景不能继续分派登记，连接就绪断言超时；保留有界后台任务。握手已完成的对照仍通过，避免把单个成功场景误当作无阻塞证明。

当前实现的对应测试及全量后端回归均通过。以上对照支持保留复制、预算检查及后台任务，不支持增加历史映射层、独立文件路由或新通信框架。代码与记录目前仅在本地，未提交、推送或部署。

### 008 — 按文件名打开可关闭的编辑器页签

**需求与决定。** 用户要求参考 IDE 文件展示，明确拒绝固定名为“文件”的标签。此修订替代 005–006 中单个可复用文件 tab 的 UI 决定：在现有 Flow 详情标签栏按实际文件名打开、切换和关闭文件，保留原有会话、概览与执行记录。没有增加独立浏览器窗口、编辑能力或文件浏览器。

**文件名随显示链接传递。** 既有文件服务只返回随机链接 ID 和文件字节，没有文件名；链接文字可能只是“源码”，不能据此推断实际 basename。选择在 worker 替换目的地址时附加 `#name=<百分号编码的 Path.name>`，而不新增 `HistoryPage.file_refs`、映射字段或文件元数据 RPC。basename 同原链接一起计入既有长度预算，目录不进入新 URL，HTTP/RPC 读取仍只使用随机 ID，片段不发送给文件端点。保留原链接文字、title、原生缓存和游标。投影指纹更新为 `file-links-v2-names`，复用原有分批重读；旧链接仍可打开，以链接文字或短 ID 作显示回退。

**详情页签。** `App.tsx` 在当前 Flow 访问内保存已开文件的有序 ID/名称列表，同 ID 去重，同名不同 ID 以短 ID 区分。页签用文件图标、实际文件名和独立关闭按钮，关闭按钮与 Radix trigger 为同级元素，避免嵌套 button。聚焦页签按 Delete 也可关闭；关闭当前项先选择右侧相邻项，其次左侧，最后回到会话，焦点随之转移；关闭后台文件不切换当前内容。文件路由为 `#flow/<id>/file-<id>`，刷新、离开 Flow 或后退至已关闭文件时回到会话，不持久化或恢复文件列表。

**阅读和安全。** 长文件名省略并通过 title 保留全名，仅标签栏横向滚动；选中时连同关闭按钮一起滚入视野。预览内容铺满详情正文，使用紧凑只读/编码/大小工具栏和下载、重读图标，移除重复标题、句柄与大段说明。仍仅挂载当前文件，保留取消、迟到结果隔离、既有文本分段与实际位图解码、512 KiB 和 owner 鉴权。文件名片段在前端校验编码、长度及路径/控制/bidi 字符，只作转义文字显示。切回会话保留原阅读位置，文件阅读位置按各自页签保存。

**验证与简化对照。** 更新现有浏览器用例并增加同名/长名多文件的切换、去重与关闭验收。旧固定标签先在“文件 tab 应不存在”的断言失败；没有 basename 片段时，worker 回归无法得到实际文件名，两项均在实现后通过。以“源码”文字对应实际中文 HTML 文件名、相同 `README.md` 对应不同 ID，以及关闭最左/最右/后台/最后一个文件作为可区分错误实现的输入。实际浏览器还检出并修复了布尔焦点标记早于 hash 路由消费、快速后退重复同一 tab 值以及手机只滚动 trigger 导致关闭按钮在屏外的问题；最终使用目标页签标记、失效路由归一化和整个文件项滚入视野，不增加路由库、全局状态库或通信层。

已执行前端完整 Playwright、单测、类型、格式与生产构建，以及后端历史改写、双端 Session、会话 API 和文件代理相关回归、Ruff 与应用/测试 Mypy。浏览器用例实际覆盖桌面和手机的关闭按钮完整可见、局部滚动、焦点、前进/后退、会话阅读恢复、图片、安全源码、下载与错误；截图已查看。截图为 API 拦截下的真实组件渲染，不作为生产 Access 或线上 Worker 验收。证据为 `.amp/in/artifacts/editor-file-{browser,backend}.log` 和 `editor-file-{tabs,text,image,binary,error}-{1440,390}.png`；源码键盘激活截图中的绿色框为保留的 focus-visible 提示。当前代码仍为本地修改，未提交、推送或部署。

### 009 — 代码和文本使用原文件行号

**需求与实现。** 用户要求代码与文本文件使用带行号的代码渲染。在 008 的文件名页签内，`FileViewer.tsx` 为已有 `TextWindow` 启用行号；普通会话和工具代码保持原外观。文本、Markdown、HTML、SVG 继续显示转义源码，不增加编辑器或高亮依赖，不执行文内内容。每段仍受既有字符和行数预算限制，文件读取与安全边界不变。

**行号和复制。** `windowing.tsx` 仅挂载当前段的源码行，行号由段起点之前的实际换行数计算；空行照常计数，保留缩进、CRLF 和全部源码字符，长行软换行不增加行号，字符预算切开的同一行仍沿用原行号。`styles.css` 用两列网格对齐行号与源码，行号通过不可选取、`aria-hidden` 元素的伪元素显示，不混入源码 DOM 文本、复制内容或辅助阅读文本。空文件显示第 1 行。没有为行号增加单独的读取、缓存或路由层。

**分段边界决定与对照。** 既有 `textEnd` 在达到行数预算时把最后一个换行留给下一段，直接添加行号会让下一段从前一行的空尾开始。改为把分隔符保留在前一段，下一段直接显示下一源文件行；字符预算边界不拆开 CRLF 或 Unicode 代理对。在独立进程中仅把 `end = i + 1` 改回 `end = i`，对同一组 LF／CRLF 输入比较，旧边界从第 200 行分隔符开始，当前边界从第 201 行正文开始。另验证空文本、混合空行、长单行、字符边界上的 CRLF／代理对及上限大小的全空行文件：逐段重组与原文相同，字符与挂载行数预算均保持。证据为 `.amp/in/artifacts/numbered-file-boundaries.log`。

**验证与交付。** 新浏览器验收先在旧实现中因缺少行号失败；实现后，桌面和手机验证原文件连续行号、空行、缩进、软换行、跨字符预算的同一行、跨行数预算翻段、前后段恢复、空文件与浏览器实际复制。复制行为与原生 `pre` 一致，内部 CRLF 保留，末尾未形成可见行的换行由原生选择省略，没有添加自定义复制处理。已通过完整前端浏览器回归、单测、类型、格式和生产构建，实际查看普通源码、下一段及超长源码在两种宽度下的截图；构建仍只有原有非阻断的 chunk 大小提示。标准脚本已更新本地 `console_static` 并重新构建 wheel/sdist。日志为 `.amp/in/artifacts/numbered-file-{before,browser,build}.log`，截图为 `numbered-file-{code,next,long}-{1440,390}.png`。浏览器仍使用 API 拦截，未验证生产 Access 或线上 Worker；没有后端行为改动。所有修改仍仅在本地，未提交、推送或部署。

### 010 — 2026-11-15 — 与最新 main 整合

**触发与决定。** 按用户要求，先保存完整本地工作副本，再将当前变更重放至 [main 的模型与推理强度设置提交](https://github.com/b1indsight/agentflow/commit/847f03cbcae8bca97f366e872ced9b43e1c43644)，解决代码与文档冲突。保留 main 的 wire26、固定 ToolCall 包与 `item.execution()`、会话响应缓存、模型／推理强度设置及雾蓝样式；保留 006–009 的正文发送副本改写、实际文件名多页签及行号预览。文件内容仍完整读取、仅活动文件挂载、不缓存，不接入会话响应缓存。

**语义合并。** 路由同时保留设置页与文件页签；文件页签沿用 main 的颜色、图标和连续细边框，整个文件项画选中边框，避免内部按钮重复描边。历史处理继续异步登记文件，通过后台任务释放 Session driver，并在连接关闭时回收。共用测试 fixture 改为提供异步 `read_history`，同步历史脚本在线程内执行。浏览器安全断言限定到不可信源码容器，排除 main 新增的翻段按钮自身 SVG 图标，仍检查注入节点不存在及脚本未执行。自动合并重复出现的文件表建表语句已去重。

命名与分支逻辑完全采用 main 的[直接处理联合输出决定](flow-task-input.md)：复用推理结果和 delivery，不恢复本地旧版 `FlowNaming`、`branch_name` 列、占名索引或数字后缀。该范围的实现与测试与 main 一致。本次整合未引入新的产品机制，因此不另做机制消融；既有对照仍见 007–009。

**验证。** 已通过完整前端浏览器回归、单测、类型和格式检查，以及 Ruff、应用与测试 Mypy、差异空白检查。标准脚本已重建本地控制台静态资源及 wheel/sdist，核对 wheel 静态资源与本地逐字节一致，并包含当前 `history_links.py`。实际查看普通源码、下一段、超长行、多文件、图片、二进制和离线状态的桌面与手机截图；横向溢出、焦点和脚本不执行另由浏览器断言检查。前端日志为 `.amp/in/artifacts/main-sync-browser-final.log`，构建日志为 `main-sync-build.log`，截图为 `main-sync-{numbered-file,editor-file}-*.png`。

首轮后端全量运行的安装 fixture 在复制 `app/` 内前端依赖时，与并行构建的 `npm ci` 删除／重建依赖发生冲突；错误发生在 `tests/test_worker_cli.py` 的准备阶段。没有为此修改产品或放松测试；构建完成后用最终代码重新运行，完整 PostgreSQL 后端回归通过，保留套件自身的一项跳过及 Starlette 既有弃用提示，日志为 `.amp/in/artifacts/main-sync-backend-final.log`。结束前再次 fetch 确认远端 main 未前进，工作副本无剩余冲突。浏览器仍使用 API 拦截，未验证生产 Access 或线上 Worker。所有修改仍仅在本地，未提交、推送或部署；整合前完整快照保留在本地备份 bookmark。
