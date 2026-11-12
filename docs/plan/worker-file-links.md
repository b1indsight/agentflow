# Worker 文件链接

## 目标与方案

为后续在线文件查看提供 API：worker 显式登记可读文件，server 返回链接；读取链接时由 server 向原 worker 查询文件字节。此功能不添加 WebUI、MCP 工具或 CLI，也不上传或缓存文件副本。

采用现有认证 WebSocket，登记与读取独立于 assignment。worker 只发送随机文件句柄；server 保存 `link_id → worker_id + file_id`，不接收本地路径。HTTP 沿用 Cloudflare Access 单 owner 鉴权，持有链接不代表获得权限。

实现顺序为协议与两端存储、连接消息分派、worker 登记方法、HTTP 查询，再验证端到端和错误边界。保留现有任务消息顺序、心跳、取消及恢复机制。没有引入新依赖。

## 使用

在运行中的 `WorkerRuntime` 所在事件循环调用：

```python
from pathlib import Path

link = await runtime.client.register_file(Path("/path/to/report.txt"))
# /api/files/0123456789abcdef0123456789abcdef
```

- `WorkerClient.register_file(path: Path) -> str` 返回 **server-relative URL**；用 server 的 HTTP(S) origin 解析。连接未就绪或已断开时抛出 `FileFailure("worker_unavailable")`。
- 相对本地路径按 worker 进程当前目录解析；登记时解析符号链接，固定解析后的路径和设备/inode。同一身份重复登记或响应丢失后重试返回同一链接。
- `GET /api/files/{link_id}` 在 owner 鉴权成功后读取原 worker；成功返回原始字节，包含空文件及二进制文件。后续 UI 可使用 `fetch` 消费内容。
- 响应固定为 `application/octet-stream`、`Content-Disposition: attachment`，并带 `no-store`、`nosniff`、`no-referrer` 和 sandbox CSP。HTML/SVG 等内容不在服务端 origin 执行。
- URL、HTTP 元数据、协议和错误不含 worker 路径或文件名；**文件正文按原样返回**，若正文自身含路径或秘密不会自动脱敏。登记文件的调用者负责选择可公开给 owner 的内容。

## 存储与接口

- `app/worker/state/file_links.py`：`LocalFileLinks.register(path) -> str` 与 `read(handle) -> bytes`。`file-links.json` 位于 worker root，以 0600 原子写入，沿用 worker root 独占锁。每个随机句柄对应 `LocalFile(path: Path, device: int, inode: int)`。线程锁串行化注册表更新。
- `app/persistence/file_links.py`：`FileLinkStore.register(worker_id, file_id) -> FileLink` 以 `(worker_id, file_id)` 唯一键幂等登记；`get(link_id) -> FileLink | None` 查询路由。`FileLink` 只有 `link_id`、`worker_id`、`file_id` 三个字符串字段。数据库不保存路径、文件名、正文。
- `app/contracts/protocol.py`：`FileRegister` 携带文件句柄，`FileRegistered` 返回链接或错误；`FileQuery` 请求句柄，`FileResult` 返回 base64 字节或错误。四种消息都以 `request_id` 和 `connection_epoch` 关联。
- `app/controler/workers/files.py`：`FileService` 保存在线连接和临时查询；登记身份来自已认证连接，读取响应必须匹配具体连接、epoch 和请求 ID。未知、旧连接和其他 worker 的回复不能完成查询。
- `WorkerConnection` 在独立有界任务里登记，立即分派读取结果；`WorkerClient` 独立处理登记回执和读取任务。慢登记或文件读取不占用任务执行消息队列。

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

本次使用 **wire v17**，避免与仓库文档中已废弃的 Amp v16 快照复用版本号。两端必须同时使用当前协议；v15/v16 均拒绝。journal 仍为 v4，workspace manifest 仍为 v1；完整 schema 新增 `worker_file_links` 表，遵守现有开发期无历史迁移策略。

## 实施与验证记录

### 001 — 文件链接登记和按需读取

已实现上述接口、持久映射、身份校验、读取限额及 owner HTTP 路由。选择服务端返回相对链接以避免增加 public-origin 配置；选择两端保存映射以避免把路径交给 server。与直接让 HTTP 传入路径相比，worker 仅能返回已登记身份；与保存文件副本相比，查询得到 worker 当前内容，但需要 worker 在线。

已执行真实 PostgreSQL 下的 `tests/test_file_links.py`、`tests/test_file_proxy.py` 和 `tests/test_conversation_api.py`：覆盖空文件、二进制、大小边界、并发重复登记、忙碌与空闲 worker、两端对象重建、owner 鉴权、路径不出现在消息/响应元数据、替换/删除/不可读/FIFO、跨 worker 和旧 epoch 回复、超时、断线、并发限制、慢登记不阻塞执行消息、HTTP 断开清理。消息传输采用内存队列，HTTP 使用 ASGITransport；不将这些结果等同于真实公网部署验证。

全量 PostgreSQL pytest、Ruff、Mypy、`uv build --no-sources` 和 `flowtool --help` 均通过。前端 `npm run format:check`、`npm test` 和 `npm run build` 通过；Playwright 默认端口 5174 被现有进程占用，使用临时配置仅切换到 55178 后全套通过，未停止原进程或修改仓库测试配置。

### 002 — 真实传输和简化对照

- 使用本机临时 Uvicorn、真实 WebSocket 客户端及独立 PostgreSQL schema 验证登记和 HTTP 读取：524288 字节二进制内容逐字节一致，无 owner assertion 返回 401，响应无文件名。认证签名使用本机测试 issuer，未验证真实 Cloudflare、公网或部署环境。
- 在临时 Python 进程中替换 `file_links._read`，仅移除 `expected` 身份比较后运行 `test_links_reject_file_substitution_and_growth`，测试因替换文件被接受而失败；正常实现通过，因此保留身份检查。
- 临时替换 `FileService.accept`，将回复强制视为 pending 请求的连接/epoch 后运行 `test_file_routing_isolates_worker_identity_and_connection_epoch`。首次对照发现测试中伪造与合法回复均为空，不能识别误接收；改用不同内容后，对照返回 `b"forged"` 而失败，正常实现通过，因此保留连接与 epoch 检查。
- 所有对照仅修改该进程内对象，未改动产品源文件；最终针对性测试、Ruff、Mypy 再次通过。
