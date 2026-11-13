# Worker 文件链接

## 目标与方案

为后续在线文件查看提供 API：worker 显式登记可读文件，server 返回链接；读取链接时由 server 向原 worker 查询文件字节。此功能不添加 WebUI、MCP 工具或 CLI，也不上传或缓存文件副本。

采用现有认证 WebSocket，登记与读取独立于 assignment。worker 只发送随机文件句柄；server 保存 `link_id → worker_id + file_id`，不接收本地路径。HTTP 沿用 Cloudflare Access 单 owner 鉴权，持有链接不代表获得权限。

实现顺序为协议与两端存储、连接消息分派、worker 登记方法、HTTP 查询，再验证端到端和错误边界。保留现有任务消息顺序、心跳、取消及恢复机制。没有引入新依赖。

## 使用

在已完成连接注册的 `WorkerRuntime` 所在事件循环调用；不要在 Session 的串行回调内等待此方法：

```python
from pathlib import Path

link = await runtime.register_file(Path("/path/to/report.txt"))
# /api/files/0123456789abcdef0123456789abcdef
```

- `WorkerRuntime.register_file(path: Path) -> str` 返回 **server-relative URL**；用 server 的 HTTP(S) origin 解析。连接未就绪或已断开时抛出 `FileFailure("worker_unavailable")`。
- 相对本地路径按 worker 进程当前目录解析；登记时解析符号链接，固定解析后的路径和设备/inode。同一身份重复登记或响应丢失后重试返回同一链接。
- `GET /api/files/{link_id}` 在 owner 鉴权成功后读取原 worker；成功返回原始字节，包含空文件及二进制文件。后续 UI 可使用 `fetch` 消费内容。
- 响应固定为 `application/octet-stream`、`Content-Disposition: attachment`，并带 `no-store`、`nosniff`、`no-referrer` 和 sandbox CSP。HTML/SVG 等内容不在服务端 origin 执行。
- URL、HTTP 元数据、协议和错误不含 worker 路径或文件名；**文件正文按原样返回**，若正文自身含路径或秘密不会自动脱敏。登记文件的调用者负责选择可公开给 owner 的内容。

## 存储与接口

- `app/worker/state/file_links.py`：`LocalFileLinks.register(path) -> str` 与 `read(handle) -> bytes`。`file-links.json` 位于 worker root，以 0600 原子写入，沿用 worker root 独占锁。每个随机句柄对应 `LocalFile(path: Path, device: int, inode: int)`。线程锁串行化注册表更新。
- `app/persistence/file_links.py`：`FileLinkStore.register(worker_id, file_id) -> FileLink` 以 `(worker_id, file_id)` 唯一键幂等登记；`get(link_id) -> FileLink | None` 查询路由。`FileLink` 只有 `link_id`、`worker_id`、`file_id` 三个字符串字段。数据库不保存路径、文件名、正文。
- `app/contracts/protocol.py`：`FileRegister`/`FileRegistered` 使用 `server.file_register`，`FileQuery`/`FileResult` 使用 `worker.file_read`，携带随机句柄、链接、base64 字节或固定错误。它们是统一协议的 typed call/result，以 `message_id`/`reply_to` 关联，epoch 位于顶层信封。
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

合并当前 main 后使用 **wire v25**，两端必须同时升级，旧 wire v17/v24 不在线互通。保留 main 的 journal v5、执行 ResultInbox v1 / Control 通知 inbox v2 和 workspace manifest v3；新增 `worker_file_links` 表。main 的 worker/workspace 复合主键不允许文件表继续引用单独的 worker_id，因此文件路由保存已认证的逻辑 worker ID，不绑定可回收的 workspace 行。既有数据库与 journal 的升级限制仍见[升级说明](../operations/upgrade.md)。

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
