# Control 职责归属与重复实现收敛 — Feature record

**Stage:** Implemented — 实现、消融与本地验证完成，待独立代码审查及 CI。
**PR:** [#73](https://github.com/b1indsight/agentflow/pull/73)。
**Bookmark:** `refactor/control-ownership-and-duplication`
**基线:** `main` / `0c647bd3`（PR #72），2026-10-10。

## 1. Initial plan

### 背景、目标与验收条件

代码准则审查发现四项维护问题：coordinator 直接修改 review_batches 表；Judge 答复和
结果评论分别实现幂等投递规则；ResultEffects 同时管理 PR 生命周期；WorkerGateway
同时拥有持久结果扫描、并发恢复和实时消息处理。
依据：[coordinator](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/coordinator.py)、[effects](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/effects.py)、
[gateway](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/gateway.py)、[代码准则](../../.agents/skills/code-principles/SKILL.md)。

目标是让 SQL、评论投递、delivery 生命周期和结果恢复各有明确归属，删除需要同步维护的
核心重复。验收以当前行为回归和职责边界为依据，文件长度不作为拆分标准。
范围包括关联生产代码、测试替身、职责说明；沿用数据库结构、消息协议、配置和锁顺序。

### 拟议行为

正常交付、Judge ANSWER、取消、close/reopen、合并和结果重放保持现有语义。
远端响应丢失后继续只读查询评论 marker 或 PR 身份；确认未发送时才允许重试写入。
实时 terminal 消息和后台恢复通过同一个结果应用入口及同一份逐 job 互斥。
网络等待期间释放数据库事务，I/O 后仍在锁内复查领域状态。

### 技术设计与模块边界

1. **ReviewStore 拥有批次持久状态。** 增加具体的上下文失效、答案发送授权、确定拒绝后
   复位操作，替换 coordinator 的裸 SQL。方法表达所需批次身份及当前状态，授权在现有
   Flow 锁和事务内进行；复位核对批次仍属于该答复，避免旧调用修改已变化的状态。
   不引入任意列名更新 API；coordinator 继续决定何时调用这些操作。
2. **共享评论投递规则。** 在 control 内集中普通评论和线程回复共同的 marker 查询、
   已授权但回执未知时禁止重发，以及确定未发送错误的判定。调用方在自身领域复查后
   持久授权，再执行 POST；查询到回执后由各自状态所有者完成提交。保持两个具体调用点
   的事务和重试语义，不引入通用 effect 引擎或可配置模式矩阵。
   Judge 保留 review_batches.answer_dispatched 和批次 marker；结果保留 effect ledger。
   本决策延续 [control triage 第 6 项](control-triage.md#6-2026-10-08--取消独立-triage-job已批准实施中)，
   不创建虚构 job、worker 或第二套持久账本。封装以实际变化点为限，稳定接口不暴露 SQL。
3. **Delivery 生命周期集中管理。** 由 lifecycle 模块拥有 PR 身份核验后的本地绑定、
   close 和 merge 提交；统一 effects 两处重复的 repo/number/head repository/branch/base/SHA
   身份判断。整合现有 pause_delivery 和 record_merge 的归属，协调 service、reconciler、
   effects 的调用，避免 lifecycle 与 service 的循环导入。
   普通关闭恢复与取消后恢复分别保留领域前提、版本 fence 和已有通知时机；提取共同
   核验和状态提交，不把两条路径的不同条件合并成一组开关。
   effects 负责远端操作和回执。通用 PR 校验移出 FlowToolService，错误在调用边界映射，
   结果副作用不再依赖工具服务来表达生命周期冲突。
4. **结果处理组件拥有恢复与互斥。** 提取一个具体的结果处理组件，管理有界扫描、
   cursor/ceiling、公平遍历、固定并发池、逐 job 锁和持久 terminal 应用。
   runtime 构造唯一实例并直接调用后台恢复；gateway 注入并调用同一实例处理实时结果，
   保留连接鉴权、assignment fence、持久接收和 ACK。组件复用 ResultApplication 及
   coordinator 的失败落库语义；已接收的结果按数据库事实恢复，不依赖 socket 在线。
   恢复完成后的派发通知由 runtime/gateway 各自触发，组件不持有 gateway。

### 备选方案与取舍

仅增加相互引用注释不能解决核心投递协议的同步维护；把 Judge 接到完整 job 结果账本
会重新引入已删除的模型。选择共享投递规则并保留领域状态所有权。
仅移动 recovery 方法、继续让实时和后台各有一份锁，会失去互斥；选择单实例结果处理。
把所有 PR 路径合成带多个布尔参数的函数容易隐藏前提；保留具体入口，共享必要规则。

### 风险与控制

主要风险是响应丢失后重复 POST、旧调用清除新状态、取消后错误绑定 delivery、
实时 ACK 和后台扫描并发重复应用，以及模块循环依赖。保留持久发送标记、I/O 后 fence、
逐 job 互斥和应用标记的原子提交；用可控等待和真实 PostgreSQL 覆盖竞争。
提取不改变 retry/backoff、扫描预算及异常隔离，不向日志写入凭据或完整消息。

### 实施顺序

1. 对照四条调用链列出事务、远端写入和恢复分支；复用行为回归，仅对缺口补最小测试。
2. 收回批次 SQL并统一评论投递，迁移 Judge 和结果评论/线程回复路径。
3. 收拢 delivery 生命周期和 PR 身份校验，更新 service/reconciler/effects 调用。
4. 提取结果处理组件，统一实时和后台互斥，更新 runtime/gateway 及测试替身。
5. 删除被替代入口和无效依赖，更新组件职责说明与本记录的实施时间线。

### 验证与消融

- 复用 control_triage、result_recovery、recovery_concurrency、gateway、delivery_receipts、
  pr_lifecycle、merge、cancel_drain、review_resolution 和 workflow_transactions 测试。
- 重点证明 POST 成功但响应丢失只查询回执；确定未发送可以重试；close/merge/cancel
  与远端读取交错不提交过期状态；实时消息和恢复同时到达只应用一次并正确 ACK。
- 更新边界检查，覆盖 control 不直接更新业务表、effects 不依赖 FlowToolService、
  lifecycle 无反向编排依赖；行为回归优先，避免只断言文件布局。
- 实施后、自动代码审查前，逐项临时移除发送前持久标记、I/O 后版本 fence、共享 job 锁，
  对固定竞争场景运行消融并恢复源码，记录预期失败及恢复通过结果。
- 最终运行真实 PostgreSQL 全套 pytest、Ruff、Mypy、uv build 和 diff 检查。
  不把数据库缺失导致的跳过视为通过，不重复增加已有测试覆盖的排列。

### 开放问题与后续工作

没有必须先回答的产品问题。具体 helper 名称与参数在实施时依据两个真实调用点收敛；
若发现保持语义必须新增持久字段或协议变更，先在本 PR 追加设计修订再实施。
多实例 control 的跨进程投递协调、插件持久化投递不纳入本次范围。

## 2. PR change timeline

### 1. 2026-10-10 — 批次存储与评论投递收敛

- coordinator 的上下文清空、发送标记设置与复位 SQL 已迁入 ReviewStore。
  `invalidate_context` 只处理相同冻结上下文且尚未发送的批次；`authorize_answer` 在
  Flow 锁内核对有效批次、上下文 digest 和发送标记；`reject_answer` 开启短事务，
  避免旧拒绝响应清除替换上下文的发送标记。
- `comment_delivery.deliver_comment` 集中 marker 查询、未知回执禁止重发、调用方授权
  后发送的顺序；`definitely_rejected` 集中确定拒绝分类。Judge ANSWER、结果评论和
  线程回复共用这些规则。批次标记与 effect ledger 各自保留，未新增 job 或账本。
- coordinator 的 ANSWER 路径复查远端 head/base，再持久化决定并授权 POST。上下文
  失效在事务结束后返回等待，避免异常退出事务把失效写入回滚。已有 hook 时机保留：
  marker 恢复不重新触发 on_review，新发送并完成批次才通知。
- 测试先行：旧代码运行新增批次状态回归得到 `invalidate_context` 缺失的预期失败
  （1 failed、7 passed）。新增确定拒绝重试、并发答案和旧上下文拒绝回归；初轮批次、
  评论及线程恢复相关测试 96 项通过。

### 2. 2026-10-10 — PR 生命周期与结果恢复归属

- lifecycle 集中创建 PR 的身份核验、取消后及普通关闭后的本地绑定与版本 fence，
  并接管原 ControlService.record_merge；service 和 reconciler 调用同一合并转换。
  两种创建恢复保持具体入口，保留各自前提、状态和通知语义。
- open PR 校验迁出 FlowToolService，ResultEffects 使用领域交付冲突异常，删除
  工具服务依赖及函数内反向导入。effect 仍拥有远端请求和回执。
- ResultProcessor 接管逐 job 锁、终态应用、扫描 cursor/ceiling、公平遍历、固定
  并发池和异常隔离。Runtime 创建单一实例并直接运行恢复，Gateway 注入该实例，
  持久接收后按数据库事实应用，确认提交后 ACK；stop 期间的迟到结果也走同一入口。
- 保留 coordinator 的未完成执行失败处理；终态处理返回持久 job 状态，避免根据
  传输消息推测 ACK 状态。测试替身通过 make_gateway 显式组合处理组件。
- 先迁移恢复测试，旧代码因 ResultProcessor 缺失出现预期收集失败。实现后相关
  并发恢复、Gateway、远程闭环、批次和生命周期测试 57 项通过。
- 扩展既有边界检查，并添加过期创建观察不得修改新 intent 的 PostgreSQL 回归。
  修正一处测试误判：SQL 边界检查原把工具 execute 也算作数据库调用，现检查裸连接访问。

### 3. 2026-10-10 — 消融与最终验证

使用 `.cache/2026-10-10/control-ownership-ablation.py`，每次只改变一项机制，运行
同一回归，finally 恢复完整源码再复验。三个基线和恢复版本均通过，消融版本均失败：

| 移除的机制 | 回归场景 | 实际失败与决定 |
| --- | --- | --- |
| 答案发送的持久标记 | 两个调用均未查到 marker，首个 POST 尚未返回 | 第二个调用也进入 POST 等待，出现预期超时；保留 |
| 创建观察提交时的 delivery version fence | 读取远端后，本地 intent 版本已改变 | 不再拒绝旧观察，错误绑定 PR；保留 |
| 实时与恢复共用的逐 job 锁 | 两个调用同时应用同一 inbox 结果 | 两个 job 产生三次结果应用；保留 |

重现命令（在本工作区，使用仓库已有 Python 环境及本地 PostgreSQL）：

```sh
FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool@127.0.0.1:55419/postgres \
  uv run --project /Users/b1indsight/personal_work/agentflow --no-sync python \
  .cache/2026-10-10/control-ownership-ablation.py
```

本地记录位于同一日期 cache 的 `*-baseline.log`、`*-ablation.log`、`*-restored.log`
及 `control-ownership-ablation-results.json`，不纳入版本控制。

- 恢复后最终真实 PostgreSQL 全套回归：**850 passed，56.31s**，无跳过；日志为
  `.cache/2026-10-10/control-ownership-tests.log`。唯一 warning 是既有 Starlette
  TestClient 的 httpx 弃用提示。
- Ruff 全量通过；Mypy 检查 162 个源文件通过；`uv build --no-sources` 的 sdist 和
  wheel 构建通过。另以 jj 的实际 workspace diff 检查新增行空白，通过；该 workspace
  共享 Git 元数据，不能用 colocated Git 的工作区差异替代 jj 差异。
- 代码准则复核：共享规则只抽取实际同步变化点；具体生命周期入口保留差异，未增加
  模式参数、通用调度器、数据库迁移或协议字段。职责说明与 changelog 已同步。


### 4. 2026-10-10 — 按用户要求删减重构中的额外层次

用户指出重构不应增加代码。第一版生产代码相对 main 净增加 107 行，新增的封装没有
减少足够的概念；本次决定替代条目 1、2 中的评论回调抽象和独立 ResultProcessor：

- 删除 ResultProcessor、两份专用 Protocol 和 ResultInvalid。恢复扫描与共享锁由已有
  ResultApplication 拥有，不再重复解析持久消息或经 coordinator.fail 转发失败结果。
  Gateway 直接调用已有 coordinator 结果入口，在 ACK 边界核验已提交。
- 删除 comment_delivery 和 find/send/authorize 三回调。批次答案直接执行自己的事务授权；
  结果评论和线程回复保留一个共同执行分支。只把确定拒绝判定放回 GitHubAdapterError，
  少量相似的 marker 查询顺序用交叉引用说明，避免误抽象。
- 将取消后和关闭后 PR 创建回执绑定合为 lifecycle 的同一个入口，按已锁定的领域状态
  决定后续转移；不增加模式参数。过期 delivery version 的校验仍在提交边界。
- 删除 make_gateway 测试包装器；并发测试直接验证现有 ResultApplication 的共享入口。

生产源码 app/ 相对 PR 基线增加 285 行、删除 301 行，净减少 16 行；相对第一版减少
123 行，删除两个新增模块。文档和回归测试单独计数，不用删测试来抵消生产代码增长。
关键行为保持：未知 POST 不重发、过期观察不绑定、实时和恢复同 job 不重复应用。

本次验证：相关回归 77 passed；全套 850 passed，56.91s，无跳过，仅既有 Starlette
弃用提示。Ruff 通过，Mypy 检查 160 个源文件通过，sdist/wheel 构建通过。
前述三项消融在精简实现上重跑，均为基线通过、移除机制失败、恢复后通过；脚本的共享锁
目标已改为 results.py。删除回调层和 ResultProcessor 后全套行为仍通过，因此不保留这些
额外抽象；逐 job 锁等机制由消融证据支持。
