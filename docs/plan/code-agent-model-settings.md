# Code agent 模型设置 — Feature record

新增顶层设置页，第一项按 Codex / Amp 类型保存全局默认模型。建议只在新建编码会话时使用新设置，不中断正在运行的任务，也不强制改变已有会话的模型。

**Stage:** 实施完成，进入 PR 交付；实际支持范围及证据见下方实施记录，Initial plan 保留为原始方案，未部署。
**PR / Bookmark:** `feat/code-agent-model-settings`，独立 jj workspace `.cache/code-agent-model-settings`；[实现 PR（按 head 查询）](https://github.com/b1indsight/agentflow/pulls?q=is%3Apr+head%3Afeat%2Fcode-agent-model-settings)。
**讨论：** [设置页需求](https://ampcode.com/threads/T-01a1dabd-c728-7596-ad83-aaa883329cf9)。
**基线：** 实施起点为 [9478797cbb64](https://github.com/b1indsight/agentflow/commit/9478797cbb64)，发布前同步至 [736a16c33c37](https://github.com/b1indsight/agentflow/commit/736a16c33c37)。共享工作区有其他任务的改动；不得覆盖它们或将其混入本功能。

## 1. Initial plan

### 动机、目标与范围

- 用户希望增加设置页面，首先支持按 code agent 类型选择模型。当前 [`WorkerSettings`](../../app/worker/config.py) 只选择 `codex` / `amp`；[`CodexAgent`](../../app/worker/execution/codex.py) 未显式传模型，[`AmpCodingAgent`](../../app/worker/execution/amp_agent.py) 对 start / continue 都固定传 `--mode low`。
- 建议设置作用域为本 server 全局：所有 Codex worker 共用一个默认选择，所有 Amp worker 共用另一个，配置持久化到 PostgreSQL。保存后的后续新会话无需重启 server 或 worker 即可采用新值。
- 首版不增加按 worker、仓库或 Flow 的覆盖，不切换 worker 的 agent 类型，不管理账号、凭据、reasoning effort 或 provider，不增加动态模型目录。独立 inference 和 server 本地 Bub Judge 保持原样。

### 建议页面与生效行为

1. 顶栏增加“设置”入口，访问 `/console/#settings`；提供返回看板的入口，保留原筛选与现有 Flow 深链接。设置页面的刷新只重新读取设置。
2. “Code agent 模型”区域分别显示 Codex 和 Amp 的设置。Codex 填写模型 ID；Amp 支持填写 `provider/name`，并清楚区分其原生模式选项 `low / medium / high / ultra` 与具体模型。实际开放的选项必须先经过发布版本的 CLI 校准。
3. 两项均提供“不指定”选项：Codex 保留原生默认配置，Amp 新会话保持当前 `low` 默认。不把静态建议冒充账号的实时可用模型清单。
4. 使用显式“保存”按钮，不自动保存；展示加载、读取失败及重试、未保存修改、保存中和成功状态。保存失败保留输入；过期版本返回冲突并提示先重新读取。网络中断时提示保存结果未知，通过回读核对，不自动重复提交或悄悄清空草稿。
5. 设置用于随后派发任务中的新建编码会话，即 INITIAL 和既有流程允许的 revision fallback start。同一 assignment 内固定使用其携带的值；后续新 assignment 可以读取最新值。
6. 已启动的进程不受影响；correction / revision 的 resume 不套用新全局值，沿用原生会话恢复行为。Amp continue 不再无条件强制 `low`，但须先确认固定版本的原生恢复语义；不为换模型重建会话或新增 fallback。
7. 页面说明配置只校验格式，不验证账户模型权限，也不会发起模型探测。模型不可用时保留原生执行失败及诊断，不静默换用其他模型。

页面复用现有 Button / Input、浅色绿色强调、直角控件和细分隔线，遵守 [STYLE_GUIDE](../STYLE_GUIDE.md) 与 [Web README](../../app/controler/web/README.md)，不重新设计看板和详情布局。

### 数据、API 与模块职责

**固定配置，避免通用设置框架。** 在 [`app/domain/models.py`](../../app/domain/models.py) 增加 `CodingModelSettings`：

- `codex_model: str | None`：新 Codex 编码会话的模型 ID，`None` 表示不覆盖原生配置。
- `amp_model: str | None`：新 Amp 编码会话的模型 ID 或允许的原生模式，`None` 表示当前默认 `low`。
- `revision: int`：非负设置版本，用于避免两个页面的旧表单互相覆盖；不承担 worker 配置同步职责。

在 [`schema.py`](../../app/persistence/schema.py) 中新增单行 `runtime_settings` 表：`id` 固定为 1，两个 nullable TEXT 模型字段，以及从 0 开始的 BIGINT `revision`。初始化通过幂等 CREATE / INSERT 完成，不保存密钥，不增加未使用的审计字段。

新增 `app/persistence/settings.py`，沿用现有 Repository / Database：

- `SettingsStore.get(self) -> CodingModelSettings`：读取唯一配置，初始值为两个 `None`、版本 0。
- `SettingsStore.update(self, codex_model: str | None, amp_model: str | None, *, expected_revision: int) -> CodingModelSettings`：按旧版本原子更新两项并递增版本；冲突明确返回给 API。模型标准化和校验由边界类型负责，不在 SQL 中实现另一套规则。

在 [`contracts/console.py`](../../app/contracts/console.py) 增加 `UpdateSettingsRequest`，字段为 `codex_model: str | None`、`amp_model: str | None`、`expected_revision: int`，均显式提交。拒绝额外字段、过长值、控制字符和非法选择；首尾空白移除，空字符串转为 `None`。Amp 的具体模型格式和允许模式按固定 CLI 的支持范围收窄，不开放插件 agent 或 `claude-code` 后端。

在 [`controler/console/api.py`](../../app/controler/console/api.py) 增加：

- `GET /api/settings`：返回 `CodingModelSettings`。
- `PUT /api/settings`：接收 `UpdateSettingsRequest`，成功返回保存后的配置；非法输入 422，旧版本 409，数据库不可用沿用现有 503。

复用 [`owner 鉴权`](../../app/controler/api.py) 和现有 `_require_same_origin_json`。阻塞数据库操作通过 `runtime.console.db` 执行，不改 `.env`、用户 CLI 配置或以浏览器 localStorage 充当配置来源。

### Agent 身份与模型如何到达 worker

- [`Register`](../../app/contracts/protocol.py) 独立上报 `coding_agent: Literal["codex", "amp"] | None`；编码 worker 必须声明类型，`execute=False` 的本地 inference peer 可为 `None`。同步更新 [`WorkerRecord`](../../app/domain/models.py)、worker 注册与 `WorkerStore.worker_connected` 持久化路径。不能依赖可能为空的 [`inference.capability()`](../../app/worker/execution/inference.py)。
- `AssignmentArguments` 增加 `model: str | None = None`。沿现有 `JobCoordinator.prepare_for_assignment(...) -> AssignmentArguments | PreparationState` 路径，按已认证连接的 agent 类型读取相应设置；在签发短期 secret 前完成读取，并保留原有派发授权和 lease 检查。ARCHIVE 不调用模型、无需读取模型设置。
- 不新增嵌套的“模型选择”类型、配置广播、worker 侧数据库连接或模型发现 RPC。设置读取失败时不把已配置模型悄悄替换为默认值，使用现有准备失败处理；实施时核对具体可重试分类，不扩大调度重试范围。
- [`WorkerExecutor`](../../app/worker/execution/executor.py) 从当前已绑定的 assignment 捕获模型到局部变量，仅供该任务的 start 调用使用，不修改共享 agent 实例，也不把配置混入凭据 context。
- [`CodingAgent.start`](../../app/domain/ports.py) 变为 `start(self, task: str, cwd: Path, *, output_schema: Path | None = None, context: CodingExecutionContext | None = None, model: str | None = None) -> CodingExecutionResult`。`resume`、TaskExecutor 的公共签名保持不变；inference 不传模型，行为不受影响。
- Codex 新会话有选择时使用原生 `--model`；没有选择时不增加覆盖。Amp 新会话使用已校准的模型 / 模式参数，没有选择时保留 `--mode low`。继续会话不读取新设置；不改监督、取消、MCP、凭据过滤、会话相关性及 VCS / 交付核验。
- [`App.tsx`](../../app/controler/web/App.tsx) 增加设置路由与顶栏入口；新增 `SettingsPage.tsx` 承担独立表单，沿用 `types.ts` 和 `api.ts` 的 DTO 与可取消请求，不增加全局状态库或依赖。设置页不因自身读取失败阻断看板。

### 取舍、成本与兼容

- 环境变量无法满足网页修改及即时生效；修改原生 CLI 的用户配置会影响非 FlowTool 调用；配置广播会增加连接状态和竞态。因此选择 server 持久化、assignment 携带当前值、每次 start 显式传参。
- 动态获取完整模型列表需要新增 worker / provider 能力和权限判断，超出首个设置项的范围。首版接受模型 ID 输入及有明确含义的 Amp 模式选项，不声称知道账户可用性。
- 保留一个 `revision`，用于防止旧页面覆盖新配置；不加入设置历史、推送通知、配置缓存或审计框架。两个原子字段不需要通用 key-value 注册表。
- 不为旧会话新增一套模型状态；依赖原生恢复，同时把固定版本的会话恢复语义列为实施前置检查。若无法满足“不强制换已有会话模型”，暂停对应支持并回报，不静默扩大范围。
- wire 有新字段，需要协调 server / worker 升级。已核对本地为 v24，最新 main 为 v25；实际版本从实施基线递增，不在本方案硬编码下一版本。新协议不伪造旧 worker 的类型。
- 数据库变更为新增配置表和 worker 类型列；旧 worker 类型保持未知，待新版本重新注册。按既有版本检查拒绝协议不匹配的连接，不以推理后端猜测。升级先 drain；目标部署、生产数据库变更和回滚另获授权，本地隔离测试不受此限制。未使用新表的旧代码可以忽略它，不为回滚删除数据。

### 实施顺序

1. 在保留共享改动的前提下准备基于最新 main 的独立 change。校准 Dockerfile 固定的 Codex / Amp：模型参数、模式与模型的区别、恢复会话时不传覆盖的语义。不得用本机较新版本的结果替代发布版本，不自动升级 CLI，也不通过启动真实 agent 代替只读校准。
2. 实现配置类型、单行持久化和 GET / PUT API，覆盖默认值、保存、重启后读取、版本冲突与授权边界。
3. 接入 agent 类型注册、worker 记录、协议版本与 assignment 模型选择；接入 start 参数，保留 inference 与原生 resume 边界。
4. 实现设置页、路由、表单状态和说明，更新当前行为文档；保留原 Flow 深链接、搜索和看板状态。
5. 运行相关与组合验证，检查实际桌面 / 手机页面，再按实际交付阶段决定是否提交、审查和发布。

### 验证与简化对照

以下均为拟执行的验收，尚未运行功能测试：

- PostgreSQL / API：默认空配置、保存后跨实例读取、两种 agent 设置互不串用、旧版本 409、校验失败不写入，以及 owner / 同源限制。显式网络写入结果未知时，回读不覆盖草稿或自动重发。
- worker 闭环：编码 worker 在 `inference=None` 时仍正确选择模型；infer-only peer 注册不受影响；INITIAL / 允许的 fallback 使用派发选择，correction / revision resume 不套用新全局值；设置变更不改变已派发任务。模型不可用时不能静默回退。
- 并发调用使用不同模型时彼此隔离，不改变共享 agent 对象；独立 inference 维持原参数。扩展现有 driver / protocol / executor 测试，必要时增加一个真实 Session 的端到端用例，不为每个内部 helper 新建测试。
- 前端：设置深链接、前进后退、键盘可达、保存 / 清空 / 失败 / 冲突 / 未知结果与未保存状态，原看板 / 详情入口回归。使用既有 Playwright 路由 fixture，只在测试中模拟 API。
- 视觉：1440px 和 390px 的默认、已编辑、保存成功与错误状态，截图后实际查看；检查无横向溢出，沿用直角和细分割线。
- 检查命令沿用 `uv run ruff check app tests`、`uv run mypy app tests`、相关 pytest 与较广 PostgreSQL 回归；前端 typecheck、format:check、test、build 和相关 Playwright。固定 CLI 的真实账户模型可用性若未执行，明确记录为验收限制。
- 简化对照在实施后、代码审查前进行：去掉配置版本检查，双页面旧表单用例应暴露覆盖；去掉显式模型传递，自定义模型的实际进程参数用例应失败。默认空配置作为负对照，应保持 Codex 默认和 Amp 新会话 `low`。不为这些对照加入生产开关。

### 待确认与实施时解决的问题

- **本次请确认的产品边界：** 按 agent 类型全局共享；仅用于新建编码会话；已有会话恢复不强制换模型；独立 inference / Judge 暂不纳入设置。以上为建议，尚非已批准需求。
- **实施前核对：** Dockerfile 固定 Codex `0.153.4`、Amp `0.0.1792555249-g702cd1` 的参数和恢复语义。本次只读查看的是较新的本机 Codex `0.162.1` 与 Amp `0.0.1794657642-gc1e41b` 的 help / version，其中 Codex 提供 `--model`，Amp 提供模型 / 模式选择且 `--model` 注明用于新会话；不能据此声称固定版本已验收。
- **不扩充本轮：** 实时模型目录、费用估算、账户连通性测试、按 worker / Flow 覆盖、切换已有会话模型、独立 inference / Judge 模型设置留待后续明确需求。

方案草稿由本机 Claude Code 的 `claude-opus-5-5` 辅助生成，初始化与响应模型已核验，工具和 MCP 列表均为空。主线程核对并收窄了其建议：不增加嵌套 selection、审计时间字段或另一套会话持久化。模型草稿不作为功能测试证据。

## 2. PR change timeline

### 2026-11-14 — 固定 CLI 校准后收窄 Amp 选项

- **触发与上下文：** 原方案希望支持具体 Amp 模型 ID，但明确要求以 Dockerfile 固定版本为准。只读校准 Codex `0.153.4` 与 Amp `0.0.1792555249-g702cd1` 的 version / help，未发起真实模型执行或升级 CLI。
- **选择与状态：** Codex 原生 `--model` 可用；固定 Amp 仅提供 `--mode low|medium|high|ultra`，不提供 `--model`。实现的 API 和页面仅允许这些模式或 `null`，具体 Amp 模型 ID 延后。本条收窄 Initial plan 的 Amp 模型支持，不更改固定版本和默认 `low`。
- **恢复边界：** start 显式使用当前选择，Amp continue 去掉原先无条件的 `low`，Codex resume 不增加模型覆盖。已验证 adapter 的实际进程参数构造；没有登录真实账号验证原生续跑后的最终模式，因此不把原生保存模式的恢复效果列为已验收。
- **证据：** 本线程证据目录 `.amp/in/artifacts/code-agent-model-settings/` 中的 `amp-pinned-help.txt`、`amp-pinned-continue-help.txt`，以及 `tests/test_codex_driver.py`、`tests/test_amp_driver.py`。模型权限、额度、可用性和真实 resume 不在本地模拟验证的证明范围内。

### 2026-11-14 — 配置持久化、类型注册与 assignment 下发

- **实际实现：** `CodingModelSettings`、单行 `runtime_settings`、`SettingsStore` 和 owner-only GET / PUT `/api/settings` 已接通；沿用同源 JSON 约束、数据库线程执行器及错误分类。两字段显式替换，空白归一化、非法输入拒绝；SQL 按 revision 原子写入，旧表单返回 409。
- **身份与职责：** worker 注册独立携带 `coding_agent`，持久化为 nullable 新列；编码 worker 必须声明 Codex / Amp，本地 infer-only Bub 声明 `None`。不再依赖 inference 能力推断编码类型。wire25 升到 wire26，server / worker 必须协调升级。
- **任务边界：** coordinator 用当前已认证 worker / epoch 读取对应默认值，在短期凭据签发前完成；读取失败沿用数据库失败处理，不静默换默认值。ARCHIVE 不读设置。executor 捕获 assignment 的 model，仅传给 INITIAL 或原有 revision fallback 的 start；不改共享 agent 实例，不扩充 resume / TaskExecutor 公共签名，不混入凭据 context。
- **成本与取舍：** 使用两个固定字段和一个 revision，避免通用配置框架、广播、缓存、模型发现和 worker 侧数据库。schema 初始化为幂等新增，不重写旧任务或 journal；旧 worker 类型待新版本重新注册。
- **验证：** 在隔离 PostgreSQL 中覆盖 owner / 同源、校验、跨 Database 实例与再次初始化后的读取、双页面并发写入、无 inference 能力的类型选择和 assignment 值冻结；真实 Session / 本地 Git / MCP 测试验证两类 worker 的配置到达 scripted agent。此闭环替换了外部 agent 与 GitHub 边界，没有调用真实模型或远程仓库。

### 2026-11-14 — 设置页面和显式写入结果核对

- **实际实现：** 顶栏入口与 `#settings` 深链接、Codex 模型 ID 输入、Amp 原生模式选择、全局 / 新会话生效说明。设置 API 绕开已有会话缓存，不增加依赖或全局状态库。
- **失败行为：** 显式保存；读取、校验和权限失败保留草稿。409 要求先读取对照；网络、5xx 或不完整的成功响应进入结果未知状态，锁定再次 PUT，回读仅 GET。读取失败保持锁定，读取成功保留草稿供核对；不会自动重发。迟到读取不能覆盖编辑，切换页面保留草稿和保护，整页刷新或关闭会丢失内存草稿，页面明确提示。
- **导航与样式：** 设置刷新只读设置，保留看板查询、搜索草稿、手机阶段、滚动和分页；原 Flow 深链接及缓存独立。沿用当前雾蓝主题、直角控件和 760px 断点，未按原方案过时的绿色描述更改现有主题。
- **视觉检查：** Playwright 覆盖 1440px / 390px 的默认、编辑、保存、初次读取失败、结果未知与冲突；截图均实际查看，没有发现横向截断、重叠或不可读提示。截图位于本线程 `.amp/in/artifacts/code-agent-settings-<state>-<width>.png`，API 在浏览器测试中使用 fixture，不能据截图证明后端持久化。

### 2026-11-14 — 简化对照与打包检查

- **方法：** 在独立 Python 进程中动态替换一个目标方法，再执行同一 pytest 用例；不改产品文件、不增加生产开关。移除 `SettingsStore.update` SQL 的 revision 条件后，`test_settings_are_owner_only_durable_and_reject_stale_tabs` 暴露两次保存均成功；保留原子 revision 约束。
- **传递对照：** 动态移除 Codex start 的模型转发，driver 自定义模型用例失败、原生默认负对照通过；动态移除 `WorkerExecutor` 的 start 模型转发，`test_control_gateway_and_worker_client_complete_one_remote_job` 的 Codex / Amp 两例均在接收模型断言失败。保留局部参数传递，不使用共享实例状态。
- **并发对照：** 同一个 Codex adapter 同时接收 alpha / beta、同一个 Amp adapter 同时接收 high / ultra，各自进程参数保持独立且实例没有新增 model 状态；进程边界替换为 scripted runner，未执行真实 agent。
- **证据：** `ablation-revision.log`、`ablation-model.log`、`ablation-executor.log`、`concurrent-models.log`；正常实现的组合回归结果另列，失败日志仅表示对照按预期暴露缺陷。
- **构建：** `python scripts/build_console.py`、`uv build --no-sources` 均成功；wheel 包含设置模块和生产资源，未包含 node_modules、test-results 或 tests。Vite 保留非阻断的大 chunk 提示，未为本功能扩充打包重构。没有部署或生产数据库写入。

### 2026-11-14 — 组合回归结果与交付边界

- **后端：** 在独立 PostgreSQL 17 / UTF8 数据库执行完整 `pytest -q --tb=short -ra` 通过；唯一 skip 是 Git 参数化下不适用的 jj 元数据保护场景，其 JJ 参数化仍执行。保留既有 Starlette TestClient 的 httpx 弃用提示。`ruff check app tests`、`mypy app tests` 均通过；使用共享 `.venv` 的绝对可执行路径，未同步或更改原工作区环境。
- **前端：** `npm run format:check`、`npm run typecheck`、`npm test` 与完整 Playwright 均通过。浏览器命令为 `CI=1 PLAYWRIGHT_PORT=5187 PLAYWRIGHT_ARTIFACT_DIR=<原工作区>/.amp/in/artifacts npm run test:e2e -- --workers=2`；设置用例覆盖真实模拟的 409、网络 / 503 / 畸形响应、GET 回读失败与恢复、迟到读取及导航竞态，其他页面全量回归同时运行。
- **复现与证据：** 从独立 workspace 运行，后端测试设置 `FLOWTOOL_TEST_DATABASE_URL` 指向专用测试库。日志 `full-pytest.log`、`ruff.log`、`mypy.log`、`web-format.log`、`web-typecheck.log`、`web-unit.log`、`web-e2e.log`、`build-console.log`、`build-package.log` 位于原工作区 `.amp/in/artifacts/code-agent-model-settings/`。发布前需重新准备测试库；本次专用 PostgreSQL 和校准下载目录在验证后清理。
- **交付：** 产品改动保留在 `.cache/code-agent-model-settings` 的 `feat/code-agent-model-settings`；原共享工作区仅同步本功能记录，其他任务的改动未合入、未覆盖。未推送、未创建 PR、未部署、未执行生产迁移或真实模型调用；发布前审查、server / worker 成组升级与真实账号 / resume 验收仍需后续处理。

### 2026-11-14 — 重新核对 Amp 的具体模型选择，纠正 help 推断

- **触发与更正：** 用户要求再次尝试 Amp 模型选择。第一条校准记录由固定版本 help 未展示 `--model` 推断“不提供”，证据不足；本条取代该能力判断，保留原实施范围和历史记录。
- **当前 CLI：** 本机 `0.0.1794672034-g01d6fa` 的 help 明确提供 `--model <provider/name>`，`--mode` 也接受该 ID，`--effort` 可为新会话选择推理强度。只读 `amp plugins show-agent-options --json` 返回模型目录，含 `anthropic/claude-opus-5-5`、`openai/gpt-6.1-sol`。与[官方 Model Selector 文档](https://ampcode.com/docs/model-selector)的 CLI 说明一致；目录不证明账号额度或成功推理。
- **固定旧 CLI：** 原二进制 `0.0.1792555249-g702cd1` 的 `--model` 缺值探测实际返回 `option '--model <value>' argument missing`，证明隐藏参数存在。编译二进制中的诊断文本包含账号开关拒绝信息，以及 `provider:model` / `mode=provider:model` 的旧格式；这只说明存在相关解析分支，尚未证明当前账号启用或旧版能成功执行。
- **证据与范围：** 只读 help / version、模型目录、二进制字符串和缺值解析探测；没有创建模型会话、修改账号设置、升级 CLI 或更改产品实现。新证据位于 `.amp/in/artifacts/amp-model-selection-20261114/` 的 `current-help.txt`、`current-version.txt`、`current-models.json`、`pinned-parser.txt` 和 `model-selector-docs.json`。页面仍仅开放已实现的模式选项；具体模型接入与是否更新固定版本尚未实施。

### 2026-11-14 — 按新要求升级 Amp 并接入具体模型

- **触发与取代：** 用户要求“换新版 接入， 然后模型用下拉框选择而不是手动填写”。本条取代前面的不升级 CLI、仅开放 Amp 原生模式和 Codex 手填模型的选择；保留按类型全局共享、仅新建编码会话生效、resume 不覆盖、独立 inference / Judge 不变的边界。
- **版本与实现：** `Dockerfile` 的 Amp worker CLI 固定到 `0.0.1794672034-g01d6fa`。下载官方 Linux x64 压缩包后，对解压的 ELF 可执行文件计算 SHA256，结果与官方 `linux-x64-amp.sha256` 及 Docker pin 均为 `0a8e4858ba97cf3fac9bcc551b67e32139a5de99740febfee70d639bbed650af`。不修改本机全局 CLI，不改变 Codex pin，不在运行期使用 latest 或自动更新。
- **参数与取舍：** 显式 Amp 模型以公开的 `--model provider/name` 传入 start，不与默认 `--mode low` 混用；空值保留原来的 low 默认，continue 不加两种参数。API 校验 nullable、长度和 provider/name 格式，不把预置目录当作服务端允许名单；账号权限与模型不可用由原生执行报告，不静默回退。
- **目录边界：** 两种 agent 都改为预置下拉选择，不新增模型发现 RPC、provider 凭据或实时权限判断。Amp 目录取自同版本 `plugins show-agent-options --json` 的工具能力模型；Codex 目录取自当前 CLI 缓存的可展示模型元数据，该缓存客户端版本为 `0.169.0`，并非 Docker 固定的 `0.153.4`。目录仅提供候选 ID，不证明固定 Codex 或当前账号能执行；已有非预置 ID 应继续显示和保留。
- **本批验证：** 先修改现有 driver / API 用例，旧实现按预期暴露具体模型未走 `--model`、API 拒绝 provider/name 且仍接受模式的问题；实现后针对性 PostgreSQL、API、coordinator、remote Session 和独立 inference 回归通过，Ruff / Mypy 通过。证据在 `.amp/in/artifacts/amp-model-upgrade/` 的 `backend-red.log`、`backend-targeted.log`、`ruff.log`、`mypy.log` 和官方校验记录。前端及组合验证结果随后单列。
- **复杂度与限制：** 新版本只改变固定依赖和现有参数分支，没有引入额外后端机制；红绿对照已验证该分支的必要性。本机没有 Docker，未构建或运行 Linux worker 镜像，也未发起真实模型调用、原生 resume、生产迁移或部署；读取 help 和目录不等于完成这些验收。

### 2026-11-14 — 具体模型下拉与目录契约验证

- **实际实现：** Codex 和 Amp 均使用原生下拉框，替换手动输入和 Amp 原生模式选项。`modelPresets.ts` 保存带来源版本的静态 ID / 展示名快照；不新增依赖、实时目录请求或 reasoning-effort 控件。「不指定」仍提交 `null`，已保存或草稿中的非预置值作为显式选项保留，不因目录过时被清空，ID 大小写不变。
- **边界与取舍：** 下拉框只提供候选，服务端继续接受格式合法的 nullable ID，不把快照当允许名单或账号权限证明。保留显式保存、失败草稿、版本冲突、未知结果 GET 回读及迟到读取隔离。
- **验证：** 下拉用例先在旧 UI 上暴露缺少 Codex combobox；实现后设置浏览器回归通过。在真实 owner 鉴权 API / PostgreSQL 中逐项保存并回读整个 Codex / Amp 目录，再经实际 Amp adapter 构造每个模型的 argv，核对均独立使用 `--model`、不附带 `--mode`。子进程边界使用 scripted runner，没有调用模型。完整后端 pytest、Ruff 和 Mypy 通过，唯一 skip 仍为 Git 参数化下不适用的 jj 元数据保护场景。
- **证据：** `.amp/in/artifacts/amp-model-upgrade/` 的 `catalog-api-driver.log`、`full-pytest.log`、`ruff.log`、`mypy.log`，以及 `app/controler/web/e2e/settings.spec.ts`。CLI 元数据的候选资格不证明固定 Codex 版本、账号或模型服务执行成功。

### 2026-11-14 — 设置往返的看板阅读恢复与最终组合检查

- **触发与证据：** 首轮完整浏览器回归通过，但补充设置回归暴露看板往返偶发偏移。原用例在虚拟列表尚未测量时记录 `scrollTop`，即使数值短暂相等也不足以证明阅读位置保留；改为测量后的首个完整可见卡片及偏移，同一往返场景在原恢复实现下稳定暴露卡片偏移。失败证据为 `board-logical-red.log`，未将重跑偶然通过当作修复。
- **实际修复与取舍：** 看板列直接复用已有 `WindowList.reading` 的逻辑卡片锚定，移除父层自行保存和恢复各列原始像素值的重复职责。保留卡片焦点恢复，未修改共享窗口算法或扩大挂载预算。重挂载后的未知行高度会重新估算，验收以同一卡片及其视口偏移为准，不要求内部 `scrollTop` 数字相等。
- **回归与视觉：** 同一场景连续重复运行通过，再运行完整 Playwright、前端格式、TypeScript 和单测均通过。1440px / 390px 下拉默认、选择、保存、读取失败、冲突与未知结果截图均实际查看，无可见裁切、重叠或水平溢出；截图只证明外观，接口与禁用行为由执行的测试确认。
- **最终产物：** 重新执行 `python scripts/build_console.py` 和 `uv build --no-sources` 成功，保留既有非阻断 chunk 提示。最终证据为 `.amp/in/artifacts/amp-model-upgrade/` 的 `board-logical-green.log`、`web-e2e-final.log`、`web-format-final.log`、`web-typecheck-final.log`、`web-unit-final.log`、`build-console-final.log` 与 `build-package-final.log`；截图为 `.amp/in/artifacts/code-agent-settings-dropdown-<state>-<width>.png`。
- **交付边界：** 改动仍只在独立 workspace / 同一 bookmark，本功能记录增量同步回共享工作区，未覆盖其他任务。未推送、创建 PR、部署或更改本机全局 CLI；wire26 仍须 server / worker 协调升级，Docker 镜像运行、真实模型与原生 resume 尚未验收。

### 2026-11-14 — 精简说明并增加按模型选择的推理强度

- **触发与取代：** 用户要求“缩减描述，添加推理强度的设置”。本条取代 Initial plan 与此前下拉记录中暂不提供 reasoning-effort 控件的范围；沿用全局默认、新会话生效、显式保存及独立 inference / Judge 不变的边界。
- **参数校准：** 固定 Amp 的 help 明确将 `--effort` 限定为新建模型 thread，目录的 `capabilities.efforts` 因模型不同，部分为空。Codex `0.153.4` 原生 app-server schema 将 `ReasoningEffort` 定义为非空字符串，配置键为 `model_reasoning_effort`；同版本仅执行 initialize / config/read，分别回读 high、max、ultra 均与配置覆盖一致，没有创建 thread 或调用模型。Codex 下拉仍使用较新客户端缓存的候选元数据，不据此声称固定版本或账号能成功推理。
- **数据与执行：** 增加 nullable `codex_effort` / `amp_effort`，与模型、revision 一起原子保存；PUT 显式提交四项。API 拒绝非法强度及未选具体 Amp 模型却指定强度的组合；不依赖静态模型目录判断账号权限。数据库初始化幂等加列，原模型和 revision 保留。assignment 捕获 effort，仅供 INITIAL / 已有 fallback start 使用；Codex 使用 `-c model_reasoning_effort="..."`，Amp 使用 `--effort`，null 不覆盖原生默认，resume 不增参数。仍属于未发布的 wire26 同一功能，journal / inbox 不变，部署时两端成组升级。
- **页面取舍：** 每类 agent 各有模型和强度下拉框；已知模型按快照提供强度，没有能力的 Amp 模型仅有默认项。用户切换模型时保留兼容强度，不兼容则恢复默认并等待显式保存；读取已有非预置值不静默清空。将主表单长说明缩为生效范围和兼容提示，保留冲突、未知写入及草稿丢失警告。前端实现及最终组合检查在本批完成后追加。
- **已完成验证：** 扩展原有 API / driver 用例先观察未实现时失败，再接通实现；针对性 PostgreSQL、coordinator、真实 Session / MCP / 本地 Git、fallback、停止与执行器回归通过，Ruff / Mypy 通过。旧两模型字段表升级和再次初始化均保留模型与原 revision，强度为 null。去掉 Codex effort 转发后，两个显式强度用例失败、两个默认负对照仍通过，保留直接参数传递，不增加共享 agent 状态或额外配置层。
- **证据与限制：** `.amp/in/artifacts/code-agent-effort/` 中的 `red.log`、`targeted-pytest.log`、`codex-native-config.log`、`schema-upgrade.log`、`ablation-effort-forwarding.log`、`ruff.log` 和 `mypy.log`；CLI help / schema 及目录只校准接口，不证明实际模型服务、账号授权或原生 resume 的最终强度。真实模型调用、Docker / 生产部署仍未执行。

### 2026-11-14 — 推理强度页面与真实后端组合验证

- **实际实现：** 桌面每类 agent 的模型和推理强度并排，手机按四个字段纵向排列。四项统一参与 dirty、成功响应回显、版本冲突和未知写入回读比较；仅强度不同也不能误判上次写入一致。目录逐项核对同版 Amp 元数据和已脱敏 Codex 缓存的强度列表，通过；不增加发现请求或共享配置状态。
- **完整检查：** 完整 PostgreSQL pytest、完整 Playwright、前端单测、格式、TypeScript、Ruff 和 Mypy 通过；唯一 skip 仍为 Git 参数化下不适用的 jj 元数据场景。桌面 / 手机的默认、编辑、已保存、读取失败、冲突和未知写入截图均实际检查，没有可见重叠、裁切或横向溢出。
- **真实后端：** 使用生产静态资源、真实 HTTP、原有 owner 验签（本地测试签发 JWT）和专用 PostgreSQL，浏览器保存四项并重载回读；真实 PUT 提交后丢弃响应，验证锁定及 GET 回读；另一客户端推进 revision 后验证真实 409 保留草稿，最后显式清空。最终数据库独立读取与浏览器 / API 一致。未替换 settings API，故障仅在浏览器响应链路注入；没有真实 Cloudflare 登录、GitHub 写入或模型调用。
- **证据与产物：** 本批 `.amp/in/artifacts/code-agent-effort/` 中的 `full-pytest.log`、`web-e2e.log`、`web-unit.log`、`web-format.log`、`web-typecheck.log`、`live-browser.log`、`catalog-metadata.log`、`codex-config-schema.json` 与 `codex-model-efforts.json`。生产资源及 wheel / sdist 重建成功，wheel 静态资源与当前构建逐字节一致且不含测试或 node_modules；见 `build-console.log`、`build-package.log`、`package-inspection.log`。截图为 `code-agent-settings-effort-<state>-<width>.png`，另有真实后端保存重载截图。
- **交付边界：** 改动仍只在独立 workspace / 原 bookmark；共享工作区仅增量同步本功能记录及索引，其他改动不覆盖。验证后的专用 PostgreSQL 和 scratch schema 清理，证据保留。未推送、创建 PR 或部署；真实模型、原生 resume 和 Docker 镜像执行仍未验收。

### 2026-11-14 — 合并字段标题、预选 mid 并去掉说明小字

- **触发与取代：** 用户要求每组只保留“Codex 模型 / Amp 模型”开头，不再显示重复的推理强度标题；默认显示 `mid`，删除预设快照、已读取等说明小字，设置入口仅保留图标。本条取代前两条页面记录中的可见字段标签、静态说明及“模型默认”选项，不改变后端默认值、已有会话或发布范围。
- **实际实现：** `SettingsPage.tsx` 删除重复标题、模型 ID 小字、说明和闲置读取文案，保留控件可访问名称、显式保存、未保存状态、错误、冲突及结果未知保护；`App.tsx` 的设置入口仅显示齿轮，保留 aria-label 和 title。桌面模型和强度底边对齐，手机将同组两框紧邻排列；移除无引用的说明样式。
- **默认与兼容取舍：** 未指定强度时，仅对支持 `medium` 的模型预选 `mid`，保存仍提交 CLI 接受的 `medium`；预选是未保存的草稿，不执行自动 PUT，也不覆盖已有显式强度。切换模型保留兼容强度，否则预选 `mid`；没有该档但有其他档位时，要求选定可用档位后保存。未选具体 Amp 模型或无强度能力时，空框显示“—”并禁用，不伪造可执行的 medium 组合；已保存非预设值继续保留。
- **验证与复杂度：** 扩展现有设置用例，先在旧 UI 上观察默认值断言失败，再验证 `mid` 实际保存为 `medium`、重载、显式原值保留、不兼容模型切换及禁用、无可见重复标签和图标可访问名称。前端格式、TypeScript、单测、构建及完整 Playwright 通过。默认预选沿用原草稿和兼容列表，仅增加局部选择与缺值校验，未引入配置层、API 或持久化机制；旧默认的红灯对照支持保留这项局部逻辑。
- **视觉与证据：** 1440px / 390px 的默认、选中模型、保存、读取失败、冲突及结果未知均实际截图并检查，无可见裁切、重叠或横向溢出。日志为 `.amp/in/artifacts/code-agent-effort/compact-red.log`、`compact-settings.log`、`compact-web-e2e.log` 和 `compact-build.log`，截图为 `code-agent-settings-compact-<state>-<width>.png`；浏览器 API 使用路由 fixture。本次仅修改前端及功能说明，未重跑真实 PostgreSQL / CLI 集成；上一条真实后端验证仅证明此前四字段合同，不作为本次真实模型执行证据。

### 2026-11-15 — 同步发布基线并保留两组测试合同

- **触发与上下文：** 用户要求推送并提交 PR。最新 main 已将新任务分支命名更新为 Conventional Commits 类型前缀，涉及本功能同样扩展的 coordinator 与 remote Session 测试。
- **实际合并：** 同一 feature change rebase 到 main 的 `736a16c33c37`；保留上游类型化分支的参数化用例和 `feat/implement-task` 预期，同时保留按 coding_agent 选择 model / effort、派发值冻结与真实 Session 接收值的断言。不回退命名规则，不混入共享工作区的其他改动；页面与协议实现没有因此扩展范围。
- **校验与交付边界：** 发布前重新执行真实 PostgreSQL 回归、Ruff / Mypy 及固定提交的双审查 gate，前端沿用本次完整浏览器与构建验证。执行结果见本线程 `.amp/in/artifacts/code-agent-effort/pr-*` 日志和 PR 的 CI；gate 仅 critical 阻断，其他发现完整披露，不以发现本身授权改码。wire26 协调升级、真实账号与 resume、Docker 运行和生产部署仍不属于本次推 PR。

### 2026-11-15 — 补全恢复演练的 worker 类型声明

- **触发与原因：** [PR #165 的 recovery 检查](https://github.com/b1indsight/agentflow/actions/runs/43690491444/job/132739912484) 在注册 fixture worker 时失败；新增必填 `coding_agent` 后漏改了 `scripts/smoke/recovery.py` 的直接调用，备份尚未执行。普通 pytest 未运行完整演练，因此没有覆盖这个入口。
- **修复与边界：** 演练中的编码 worker 显式声明 `coding_agent='codex'`，与现有测试 fixture 一致；保留生产接口必填约束，不以默认值掩盖缺失声明，也不改动设置页、协议或模型执行。
- **验证：** 在专用 PostgreSQL 17 和同版本 dump / restore 工具上，未修复代码复现相同 TypeError；修复后完整演练确认数据库及 HOME 恢复、原 journal 不变、复制 root 身份被拒绝。恢复 bundle、workspace、worker 注册、coordinator 和真实 remote Session 的相关回归及 Ruff / Mypy 通过。证据位于本线程 `.amp/in/artifacts/code-agent-effort/recovery-ci-{red.log,green.json,pytest.log,ruff.log,mypy.log}`。CI 的 Linux 容器演练待新提交检查，不据本地结果声称全部 CI 通过；仍未执行生产恢复、部署或真实模型调用。
