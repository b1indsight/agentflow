# 已取消 Flow 手动归档 — Feature record

**Stage:** Implemented — 实现与本地验证通过；交付目标为 `feat/cancelled-flow-archive`，未合并或部署，真实 Access 环境待验收。
**PR:** 未创建；本次仅推送独立功能分支，不包含其他任务的未提交改动。

## 1. Initial plan

### Summary and motivation

用户希望将 cancelled Flow 归档，避免已经取消的工作持续占用控制台「受阻」列。
当前控制台将 cancelled 默认映射为 blocked；已有 archive job 要求 ACTIVE Flow 和
已合并的 PR，并承担工作区清理及交付完成通知，不能复用于本次展示归档。

依据：[展示投影](../../app/persistence/console_schema.py)、
[交付归档约束](../../app/controler/workflow/archive.py)、
[现有取消与重试](../../app/controler/workflow/service.py)。

### Goals, scope, and success criteria

- 在 cancelled Flow 详情中提供「归档」操作，归档后卡片进入「已归档」列。
- 保留真实生命周期 cancelled、全部任务和会话历史；标注「已取消 · 已归档」，不显示交付成功。
- 归档记录持久化，页面刷新、服务重启及投影刷新后仍有效；列表、详情、计数和搜索保持一致。
- 不创建 ARCHIVE job，不改变 PR、Issue、标签、Projects、Worker 绑定或取消停止请求，
  不发送 GitHub 通知，也不修改原有交付归档与重试安全条件。
- 首版仅支持 cancelled；不增加 failed/active 的手动归档、批量操作、拖拽或独立撤销归档入口。

### Proposed behavior

1. cancelled 且仍在「受阻」列的 Flow，在详情概览提供「归档」按钮。
   保持现有布局和样式，不在可点击的卡片内部嵌套操作按钮。
2. 点击后按钮显示提交状态并禁止重复点击；成功后更新详情、看板和左侧导航的阶段及计数，
   保留当前 Flow、页签、搜索条件和历史阅读状态，不要求手工刷新。
3. 归档后的卡片仍可打开，生命周期显示「已取消」，归档时间可查看。
   `is:done` / `status:done` 包含该卡片，`status:blocked` 不再包含。
4. 归档只改变展示分组，不表示在途任务已停止或未决结果已完成对账；保留取消诊断与相关提示。
   历史同步仍按已有规则工作，不因展示归档删除或冻结数据。
5. 请求失败保留当前详情和历史，显示可重试错误，不提前将卡片移动到「已归档」。
   重复请求幂等；不存在的 Flow 返回 404，非 cancelled Flow 返回 409，未授权请求保持拒绝。
6. 通过现有安全重试或合并对账使 Flow 离开 cancelled 时，清除展示归档标记并恢复按真实事实分组。
   后续再次取消不会继承旧归档。已有 PR 关闭后的自动展示归档不受影响。

### Technical design and affected modules

- `app/persistence/console_schema.py`：在控制台专属持久记录中增加 nullable `archived_at`，
  默认不归档。通过集中投影刷新将手动归档的 cancelled Flow 映射为 done，
  保留 cancelled 动作和生命周期；后续事实刷新不能覆盖归档，生命周期变化则清除旧标记。
  沿用幂等 schema 初始化机制，不把展示属性加入工作流状态机。
- `app/persistence/console.py`：增加归档命令，在一个事务中按现有 workflow/publication 锁顺序
  重新读取并校验 lifecycle，持久化归档时间、刷新投影与活动记录。重复请求不重复写活动或更新时间。
  与重试和合并对账串行，避免以旧详情归档已经恢复运行的 Flow。
- `app/contracts/console.py`：摘要和详情显式返回 nullable `archived_at`，不暴露内部执行载荷。
- `app/controler/console/api.py`：增加 owner 保护的 `POST /api/flows/{flow_id}/archive`，
  返回更新后的 FlowDetail；数据库操作沿用后台线程边界，不在事务内进行网络调用。
  写入口仅接受 JSON，校验明确的跨站请求，不开启跨域写入，不增加认证绕过或浏览器 token。
- `app/controler/web/api.ts`、`types.ts`、`App.tsx`、`phases.ts`：增加类型明确的归档请求、
  详情操作及请求状态，更新分组和计数，区分取消归档、PR 关闭未合并与成功交付归档。
  处理切换 Flow 时的迟到响应，不用旧请求覆盖新详情。
- 控制台 README 与相关架构说明：说明新增的本地展示写操作，修正受影响的「全部只读」描述。
  不扩展为 cancel/retry 等管理写入口。
- `tests/test_console_api.py`、`tests/test_console_archive.py`、
  `app/controler/web/e2e/console.spec.ts`：在现有测试层验证外部行为，不为私有辅助函数逐一添加测试。

### Alternatives and rationale

- **选择独立展示归档标记**：用户决定哪些取消记录收起，业务生命周期与交付事实保持原样。
  成本是一项持久属性和一个窄范围写入口。
- **所有 cancelled 自动放入 done**：实现最少，但没有用户要求的手动操作，也不能让尚需关注的
  取消记录继续留在「受阻」；本次不采用。
- **复用 archive job 或改成 succeeded**：会混淆取消与成功交付，并引入清理及远端通知副作用；不采用。
- **仅浏览器保存隐藏状态**：无需迁移，但换浏览器或清除本地数据就丢失，服务端计数和搜索也会不一致。

### Costs, risks, and mitigations

- 新增首个控制台业务展示写入口：复用 owner Access 验签，限制 JSON 和跨站请求，补未授权及拒绝测试。
- 归档与重试并发：服务端事务重新校验状态，遵守现有锁顺序；生命周期恢复时清除旧展示标记。
- 归档掩盖未完成停止确认：不释放资源或跳过对账，详情保留取消诊断，明确归档仅改变展示。
- 投影重建丢标记或重启重复生成活动：标记独立于可重建 facts，验证刷新、重建和重复初始化的幂等性。
- 现有工作区含其他功能改动：只增加本功能所需差异，不覆盖、重新分组或提交已有工作。

### Implementation order

1. 批准方案后，实现持久标记、事务命令、投影及读取契约，先验证原生命周期和工作流不变。
2. 实现鉴权与跨站保护的归档接口，覆盖重复请求、状态冲突和恢复后的标记清理。
3. 在现有详情概览接入按钮、错误及提交状态，刷新受影响列表和计数并保留导航状态。
4. 更新受影响文档，执行后端、前端及浏览器检查，检查桌面/手机的归档前后截图。
5. 按用户授权再进行提交、推送或 PR 操作；本次规划阶段不执行这些交付操作。

迁移沿用 `init_db()` 的幂等升级方式。既有记录初始不增加手动归档；发布需在目标环境执行
已有 schema 更新流程。本次不修改线上数据库、不部署，也不自动回滚或删除归档记录。

### Validation and ablation strategy

- **后端合同：** 用真实 PostgreSQL 与 owner 认证测试归档后 phase=done、lifecycle=cancelled、
  时间持久化、搜索及计数一致、历史保留；验证未授权、错误生命周期、404、重复请求和跨站拒绝。
  对比归档前后的 job、delivery、binding、stop 和 result 数据，确认无业务副作用。
- **刷新与恢复：** 验证普通事实刷新、投影重建、重复 `init_db()` 后归档仍有效且不重复记活动；
  验证 Flow 恢复为 active 后不受旧归档影响，再次取消时回到受阻。
- **浏览器：** 执行实际点击及失败重试，检查按钮提交状态、列表/侧栏计数更新、
  切换 Flow 的请求隔离，以及已取消/已关闭未合并/成功交付的标签区别。
  用现有 Playwright 工作流检查桌面与 390px 手机页面，捕获并实际查看代表性截图。
- **检查命令：** `uv run ruff check app tests`、`uv run mypy app tests`、相关 pytest；
  前端运行 `npm run typecheck`、`npm test`、`npm run build`、受影响文件的 Prettier 检查与 Playwright。
  若缺少测试数据库或浏览器环境，明确记录限制，不将跳过视为通过。
- **Ablation：** 在临时验证中移除 cancelled 服务端校验，确认非取消状态拒绝用例失败；
  去掉投影刷新中的标记保留，确认刷新持久性用例失败。恢复后重跑受影响检查。
- **Timing：** 实现后、代码审查前执行消融；不保留临时破坏或重复测试。

### Open questions and deferred work

待批准的是上述范围：仅手动归档 cancelled Flow，保留取消状态，允许现有安全重试自动恢复展示。
没有必须新增凭据、服务或依赖的实现前提。

failed 的归档、手动取消归档、批量归档以及 cancel/retry Web 管理功能均延后，不纳入本次范围。
真实 Access 部署及目标数据库升级须在获授权的环境单独验收，不能由本地测试替代。

## 2. PR change timeline

### [001] — cancelled 展示归档的持久化、接口与详情操作

- **Context / requirement:** 用户希望手动收起 cancelled Flow，保留取消状态和全部历史；
  本次实现采用已确认的展示归档范围，不扩展为交付清理或工作流管理入口。
- **Implemented changes:** 在 `console_flows` 增加独立于 facts 的 `archived_at`，
  集中刷新函数叠加 done 展示并在生命周期恢复时清除标记；沿用活动发布与版本分配，
  归档不改写 canonical lifecycle/action。新增 owner 同源 JSON 归档接口，
  在 workflow/publication 锁内重新校验 cancelled，重复请求不重复记活动。
  详情概览提供归档、提交和错误重试状态；列表、侧栏、计数与搜索刷新，保留当前导航与阅读状态。
  以状态版本挡住迟到读取；离开原 Flow 后仍等待归档结果并刷新当前查询，不覆盖新详情。
- **Decision / consequences:** 使用控制台持久元信息，避免加入业务状态机或仅在浏览器保存。
  未增加依赖或修改任务协议；现有 archive job、重试条件、stop、binding、delivery 和远端写入路径保持原样。
- **Backend evidence:** 新增 3 项合同检查在实现前全部失败（接口缺失/归档命令缺失）；
  实现后相关 49 项检查通过。完整 `FLOWTOOL_TEST_DATABASE_URL=postgresql://localhost/flowtool_test uv run pytest -q`
  得到 849 passed、0 skipped；Ruff 和 Mypy 全量通过（195 个 Python 源文件）。
  首次完整运行使用 Unix socket URL，3 项既有 recovery-bundle 检查因缺少 TCP peer 失败；
  确认这些检查要求 loopback IP 后改用本地 TCP 连接，6 项 recovery-bundle 检查和完整测试均通过，未改无关代码。
  记录：[完整 pytest](../../.amp/in/artifacts/cancelled-archive-pytest.log)、
  [相关检查](../../.amp/in/artifacts/cancelled-archive-backend.log)。
- **Migration / concurrency evidence:** 在一次性 PostgreSQL schema 中先安装改动前的控制台 schema，
  再用现有 `init_db()` 升级；12 个并发归档请求只产生一个归档时间和一次状态发布。
  归档请求排在生命周期恢复事务后时，重新校验并拒绝旧 cancelled 状态。
  [检查记录](../../.amp/in/artifacts/cancelled-archive-storage.log)；临时 schema 已删除。
- **Frontend evidence:** `npm run typecheck`、`npm test`（2 项）、`npm run build`、
  `npm run format:check` 和 `npm run test:e2e`（16 项）通过。
  桌面/390px 手机实际点击覆盖失败重试、提交禁用、阶段/计数/标签、刷新持久性与历史保留，
  截图已检查；手机归档时间位于初始视口下方，补查滚动后的截图，无横向裁切。
  另用 2 项临时浏览器检查验证旧 GET 不回退归档，以及切换 Flow/筛选后迟到归档结果的隔离。
  这些浏览器检查使用测试 DTO，不代表真实部署或 Access 登录验收。
  记录：[浏览器回归](../../.amp/in/artifacts/cancelled-archive-browser.log)、
  [请求隔离](../../.amp/in/artifacts/cancelled-archive-browser-edges.log)、
  [格式检查](../../.amp/in/artifacts/cancelled-archive-format.log)。
- **Ablation evidence:** 仅在独立验证进程中移除 cancelled 校验，API 合同按预期失败（200 取代 409）；
  将归档投影简化为仅首次叠加，幂等/刷新合同按预期失败。
  保留两项机制；未修改生产文件进行消融，原始代码的相关检查随后再次通过。
  记录：[状态校验消融](../../.amp/in/artifacts/cancelled-archive-ablation-guard.log)、
  [投影保留消融](../../.amp/in/artifacts/cancelled-archive-ablation-projection.log)。
- **Outcome / pending:** 本地功能与验证完成，其他已有改动保留。临时测试/脚本不进入产品或永久测试集；
  提交、推送、PR、目标数据库升级及部署均未执行。真实 Cloudflare Access 登录/代理环境仍需独立验收。

### [002] — 基于最新 main 隔离归档功能的交付差异

- **Context / requirement:** 用户要求推送；原工作区同时包含其他任务的改动，不能整体发布。
  远端 main 已包含归档功能依赖的控制台修复，本次直接以该基线建立独立 jj workspace。
- **Implemented changes:** 只提取 cancelled 归档功能的源码、合同测试、浏览器回归和功能记录；
  源码与前次验证版本逐文件比较一致。部分文档的原有上下文尚未进入 main，因此按 main 的实际内容
  局部补充本功能说明，不夹带控制台架构、部署脚本或其他功能的未提交差异。
- **Validation:** 在隔离 workspace 重新执行 Ruff、Mypy、真实 PostgreSQL 全量 pytest，
  得到 839 passed、0 skipped，194 个 Python 源文件类型检查通过；与 [001] 的 849 项差异来自未带入的其他任务测试。
  前端类型检查、2 项测试、生产构建、格式检查与 16 项 Playwright 浏览器回归全部通过。
  命令分别为 `ruff check app tests`、`mypy app tests`、
  `FLOWTOOL_TEST_DATABASE_URL=postgresql://localhost/flowtool_test python -m pytest -q`，
  以及控制台目录中的 `npm run typecheck`、`npm test`、`npm run build`、
  `npm run format:check`、`npm run test:e2e`。
- **Outcome / pending:** 发布范围仅为独立功能分支；没有创建 PR、修改 main、部署或升级目标数据库。
  原工作区及其中其他任务的改动保留，真实 Access 环境仍待验收。
