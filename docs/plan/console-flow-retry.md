# 控制台 Flow 重试按钮 — 功能记录

**阶段：** 实现与本地验证完成；独立审查与发布状态见 PR，未合并或部署。
**PR：** [PR #106](https://github.com/b1indsight/agentflow/pull/106)，沿用原计划书签与 PR。

## 1. 初始计划

### 动机与目标

用户处理完受阻原因后，希望在页面上继续原 Flow，无需到控制端运行
`flowtool flow retry <id>`。在详情概览增加「重试」入口，调用现有业务服务，
明确区分请求已受理、未执行和被拒绝；保留取消停止确认、结果对账和归档绑定限制。

当前控制台只有展示归档写入口；此前计划明确延后执行状态写操作，本记录新增重试范围，
不改写原计划：[控制台范围](web-console-kanban.md)、[取消卡片归档](cancelled-flow-archive.md)。

### 范围与验收标准

- 受阻 Flow 的「概览 → 当前动作」中出现「重试」，沿用现有按钮和提示样式。
- 提交中禁用本页重试与归档；失败可再次操作，明确拒绝不改变 Flow 或任务。
- 已受理后读取服务端实际详情，刷新看板分组与计数；不提前移动卡片。
- 没有实际重试工作时不显示成功；网络中断等未知结果提示先刷新核对，不自动重发写请求。
- 后端仍以同一套事务、锁和业务规则作最终判断；不把 `blocked` 等同于可安全重试。
- 不新增依赖、表、迁移、调度器、资格预检接口、强制重试、批量操作、取消或 PR 重开按钮。
- 不操作真实 Flow/GitHub，不改动无关的本地工作；现有展示归档保留。

### 拟议使用体验

1. 用户修复错误后，打开受阻 Flow 概览，点击「重试」。
2. 按钮显示「正在提交重试…」，请求结束前本页其他写操作也禁用。
3. 服务端受理后提示「重试请求已受理，请查看当前动作及执行记录」。该提示不承诺任务已运行。
4. 未执行时提示「当前没有可重试的任务，请刷新确认状态」；拒绝时显示可理解的中文原因，
   例如等待停止确认、PR 已关闭、结果仍待对账、原 Worker 尚不可用。
5. 切换 Flow 后旧请求可完成并刷新看板，但不能覆盖新详情；会话和历史不清空。

已取消且仍受阻的 Flow 也显示按钮：沿用 CLI 已有的重新激活行为，不另加状态限制。
已做展示归档的取消 Flow 位于「已归档」，本次不额外增加恢复入口。
已合并 PR 只重试原 Worker 上的归档或已核验的失败回写，不重跑编码。
依据：[重试分支](../../app/controler/workflow/service.py#L137-L198)、
[归档限制](../../app/controler/workflow/service.py#L239-L268)、
[阶段投影](../../app/persistence/console_schema.py#L12-L24)。

### 技术设计与涉及文件

**业务服务仍拥有是否重试的决定。**

- `app/controler/workflow/service.py`：让 `retry` 明确返回是否实际受理的布尔值，
  保持已存在的 ACTIVE 无操作分支为 `False`，入队或重置失败回写后为 `True`。
  为预期拒绝增加携带类型化原因码的 `ValueError` 子类，保留原检查顺序、消息和事务。
  API 不解析异常字符串，也不捕获所有 `ValueError` 来掩盖程序错误。
  CLI 可继续忽略返回值，现有调用方式不变；不新拆一套重试服务。
- `app/contracts/console.py`：增加禁止额外字段的空 `RetryRequest`。
  成功沿用 `FlowDetail`，不增加资格字段或多余响应包装。
- `app/controler/console/api.py`：新增 `POST /api/flows/{id}/retry`，只接受空 JSON。
  复用 owner 认证；与展示归档共享同源及 JSON 校验，保留 TLS/Host 代理下的既有判断。
  不存在返回 404，无操作或业务拒绝返回 409 和稳定原因码；数据库不可用沿用 503。
  不返回原始错误、SQL、凭据或内部执行载荷。
- 通过现有 `runtime.console.db` 执行短同步数据库操作，由 `ControlService` 操作业务状态，
  `ConsoleStore` 只读取详情，不承接执行编排。构造服务时将通知回调通过
  `loop.call_soon_threadsafe` 调度至事件循环；通知仍在重试事务提交之后，
  不从数据库工作线程直接操作 `gateway` 的 asyncio Event。
- `app/controler/web/api.ts`、`App.tsx`：增加 POST 客户端及按钮，复用归档的提交后刷新、
  卸载保护和 `state_version` 防旧响应覆盖规则；后端原因码映射成中文。
  重试和归档互斥，避免连点；不引入通用命令框架。
- 更新页头「工作流只读」和 `app/controler/web/README.md` 的相应描述，
  明确控制台现在支持受限的归档与重试，不扩展其他管理功能。

通知线程边界依据：[数据库线程](../../app/controler/console/sync.py#L53-L57)、
[当前通知实现](../../app/controler/workers/gateway.py#L414-L417)。
重试后已有数据库触发器发布投影，无需新增发布机制；归档凭据校验是数据库读取，
不会在此端点执行远端操作。依据：[投影触发器](../../app/persistence/console_schema.py#L143-L160)、
[凭据校验](../../app/controler/workflow/archive.py#L14-L54)。

### 备选方案与取舍

- 只在前端新增按钮或按阶段直接改状态：不能表达拒绝，也绕开现有安全规则，不采用。
- 直接在线程中调用带原始通知回调的 `runtime.service.retry`：跨线程操作 asyncio Event，
  不采用；在线程安全回调中保留现有提交后通知，无需拆新服务入口。
- 根据异常英文消息分类：容易随措辞变化失效；采用服务拥有的类型化原因码。
- 增加 `can_retry` 和资格查询：增加维护面且不能取代提交时检查，本次按钮仅按受阻阶段展示，
  服务端拒绝会明确说明；更细的提前禁用提示后置。

### 风险与控制

- 两个页面或 CLI 并发重试：依赖现有 Issue/Flow 锁重新读取状态，不能重复排入任务；
  前端禁用只改善交互，不作为并发安全依据。
- 响应丢失：请求可能已提交，不承诺「失败就未执行」；提示刷新检查，不自动补发。
- 业务服务返回值及拒绝类型改变：保留 `ValueError` 继承关系与原消息，运行取消、修订和归档回归。
- 现有工作区包含其他未提交改动：实现时逐文件核对，只叠加此功能需要的修改。

### 实施顺序

1. 批准后先补服务的受理结果与类型化拒绝，保持重试检查和执行语义。
2. 接入认证、同源 JSON 端点及提交后线程安全通知，验证真实隔离 PostgreSQL 状态。
3. 接入详情按钮、错误提示和提交后刷新，更新相关当前行为文档。
4. 跑定向回归、类型/格式检查和构建；浏览器检查桌面及手机的受阻、提交中、拒绝和受理状态。
5. 自查与记录验证结果，按既定审查流程更新同一 PR；合并与部署另行确认。

### 验证与消融策略

- **后端：** 复用 `tests/test_console_api.py` 的 AccessIssuer 和隔离 PostgreSQL fixture，
  验证实际重排任务、无操作不报成功、代表性安全拒绝无写入、并发请求不重复入队，
  以及成功事务提交后才通知且通知运行在事件循环线程。既有业务回归继续覆盖细分安全规则。
- **请求保护：** 覆盖未授权、非 owner、跨站、非空 JSON/非法正文、404；
  保留展示归档在直连及 TLS/Host 代理后的同源测试，错误响应不得含内部错误文本。
- **浏览器：** 在 1440px / 390px 运行正式 React 与测试路由，检查错误后可再次提交、
  延迟响应期间按钮禁用且卡片不提前移动、服务端详情与分组更新、切 Flow 后旧响应不覆盖、
  会话保留、重试和归档互斥，以及页面不横向溢出。保留既有展示归档回归。
- **检查：** Ruff、Mypy、相关 pytest；前端 typecheck、单测、format:check、build 和 Playwright。
  PostgreSQL 只用明确的测试连接和隔离 schema，不对生产库运行测试。
- **可视检查：** 将默认、提交中、拒绝和受理状态截图保存至 `.amp/in/artifacts/` 并实际查看，
  最终报告附可检查的截图；浏览器测试路由不等于真实部署或 GitHub 验收。
- **消融：** 实现后、代码审查前，临时去掉无操作分类、提交中禁用或线程安全通知，
  分别验证 API、浏览器或线程断言能发现问题；恢复后重跑相关检查。
  只保留保护用户契约的回归，不为原因码映射逐项堆积机械测试。

### 开放问题与后置工作

本地已查明的边界足以进入审批，无需新增产品选项。
实现中确认测试环境可用性；不可用时明确报告验证限制。
提前显示完整资格、展示归档后的恢复按钮、CLI 无操作文案改进、批量重试均不纳入本次。

## 2. PR 变更时间线

### [001] — 2026-10-21 — 受阻 Flow 安全重试入口已实现

- **触发与实现：** 用户要求处理受阻后可从页面继续原 Flow。`ControlService.retry`
  现返回是否受理，并用 `RetryRejected` 表达稳定原因码；原异常消息、检查顺序、锁、
  事务和重排行为保留。API 增加严格空 JSON 的 owner 同源端点，与归档共享请求保护，
  无操作返回 409，成功返回已提交的 `FlowDetail`。
- **线程边界：** 保留业务服务提交后通知，API 的显式 `None` 回调通过
  `loop.call_soon_threadsafe` 通知 ASGI loop；不直接在线程中访问 gateway Event。
  相比计划中的 `partial`，显式函数符合 `Callable[[], None]`，不把调度返回的 Handle
  泄漏为业务回调返回值，也不使用类型断言。
- **页面与取舍：** 受阻概览增加「重试」，包含 cancelled 但未展示归档的 Flow。
  重试与归档共用本地提交函数，保留卸载保护和状态版本判断；同步 ref 防止同页连点，
  渲染中的按钮禁用。成功提示只表示请求已受理；已提交详情才改变分组；拒绝映射为中文，
  网络异常明确要求刷新核对，不自动重发。没有新增预检、表、依赖或通用命令框架。
- **相关文件：** `app/contracts/console.py`、`app/controler/console/api.py`、
  `app/controler/workflow/service.py`、`app/controler/web/{api.ts,App.tsx,README.md}`；
  更新 changelog，扩展现有 API 与浏览器回归。

### [002] — 2026-10-21 — 测试、可视检查与三项消融完成

- **测试先行：** 新 API 受理回归在端点实现前返回 404；新浏览器回归在按钮实现前因找不到
  「重试」失败。补齐实现后通过。浏览器使用测试路由，后端使用专用本地 PostgreSQL 实例及
  每例独立 schema；没有执行真实 Flow、GitHub 写入或生产数据库操作。
- **最终检查：** `ruff check .` 通过；`mypy app` 的 111 个源文件通过；使用
  `FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool_test@127.0.0.1:55439/flowtool_retry_test`
  运行 `python -m pytest -q`，860 项通过、无跳过（其中 console API 41 项）。
  前端 `npm run typecheck`、`test`（4 项）、`format:check`、`build` 通过；
  `npm run test:e2e` 的 23 项通过。保留一条已有 Starlette 弃用提示及构建大分包提示，
  未抑制错误或为提示扩展本功能范围。
- **覆盖行为：** owner/跨站/代理/非法正文、实际重排、无操作、双页并发只入队一次、
  提交可见后在 loop 通知，以及停止未确认、PR 关闭、结果未决、原 Worker 离线时拒绝且不改状态。
  1440px / 390px 浏览器覆盖重试与归档互斥、拒绝后可再操作、响应丢失提示、提交中不移动、
  受理后详情及分组更新、会话保留、导航后旧响应隔离及无横向溢出；既有归档回归保留。
- **可视证据：** 实际查看桌面与手机的默认、提交中、拒绝、受理四种状态，以及更新后的页头。
  截图位于本地 `.amp/in/artifacts/flow-retry-{default,pending,refused,accepted,header}-{1440,390}.png`，
  仅作页面渲染证据，不代表真实部署、Access 登录或 GitHub 恢复闭环。
- **消融设置与结果：** 一次只临时删去一项机制，运行同一回归，再恢复；不保留消融代码。
  1. 去掉 API 无操作分类：`pytest tests/test_console_api.py -q -k 'commands_require and retry and rewritten'`
     失败，重复提交错误返回 200 而非 409。保留明确的未执行响应，避免误报成功。
  2. 去掉提交中按钮禁用：`npm run test:e2e -- --grep 'blocked flow retry.*1440'`
     失败，提交中按钮仍可用。保留重试/归档互斥的禁用状态。
  3. 将线程安全调度替换成直接通知：
     `pytest tests/test_console_api.py::test_retry_commits_before_waking_workers_on_the_event_loop -q`
     失败，通知线程 ID 与 ASGI loop 不同。保留线程安全通知。
  三项均命中对应断言；恢复后重新运行全量 Python 和浏览器检查，结果如上。
  本地日志为 `.amp/in/artifacts/retry-ablation-{noop,disabled,thread}.log` 与
  `retry-final-{python,frontend,browser}.log`；这些临时证据不加入版本控制。
- **剩余边界：** 真实 Access/Tunnel、部署后的 Worker/GitHub 闭环仍须环境验收。
  按原范围不增加展示归档后的恢复入口、不改变 CLI 无操作文案；独立审查与发布状态见 PR。
