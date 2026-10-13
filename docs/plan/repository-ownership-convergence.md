# 仓库职责与写入证据收敛

**PR:** [#80](https://github.com/b1indsight/agentflow/pull/80) · **Bookmark:** `refactor/repository-ownership-convergence`
**基线:** `main` / `3c7e39a5`（#79） · **状态:** 已实现，测试复查修正与本地验证完成；外部 API 契约未明确的场景继续保留。

## 已批准范围

2026-10-12，按用户要求审计全仓库，处理实际重复的规则、无效合同与越界导入：

- [GitHub 错误](../../app/github/errors.py)：拒绝判定使用未发送或真实 HTTP 拒绝证据；
  HTTP 4xx 排除 408，GraphQL HTTP 200 的部分失败保持写入不确定，诊断状态码不证明未写入。
- [结果合同](../../app/contracts/result_models.py)：三处共用唯一 PR 创建回执规则；
  retired 将缺失/重复转为 `ResultPending`，正常执行与 INITIAL 提交仍拒绝非唯一回执。
- [启动恢复](../../app/control/runtime.py)：删除恒为零的返回计数及不可达日志，实际 sweep 保留统计。
- [任务合同](../../app/contracts/protocol.py)：删除单一子类的 `ReviewTask`，保留字段顺序与 Flow 身份校验。
- [CLI](../../app/cli.py)：控制端依赖归对应命令，worker 入口加载时不提前导入控制端。

保持数据库 schema、协议、持久 JSON、CLI 签名及重试规则；继续由既有模块拥有事务、查询和恢复。

## 2026-10-12 — 实施与行为证据

五项变更已实现。历史写入不确定性不会被后续读取拒绝清除，持久提交边界保持原有归属。
验证复用既有 SDK、Projects、结果恢复、启动恢复和进程导入测试：

- 原实现的 GraphQL 部分写入用例失败；修复后通过，真实 HTTP 拒绝仍可重试。
- 任务 JSON Schema、字段顺序与实际序列化对照一致；三种任务均拒绝不匹配 Flow。
- PostgreSQL 全量测试 900 passed；Ruff、Mypy（165 个文件）、构建与差异检查通过。
- 消融只恢复旧状态码推断：同组 6 项中 2 failed / 4 passed；当前实现 6 passed。
  保留证据来源判定；其他改动删除既有层次，无新增恢复机制需要消融。

## 2026-10-13 — 按总维护成本精简

用户指出简化 PR 净增 277 行，要求修正。压缩重复的设计说明和过程记录，保留范围、行为约束及验证证据；
删除 INITIAL 缺失/重复回执的独立参数组，retired 保留缺失回执等待、恢复后不重发；继续保留写入误判和导入边界回归。

本轮 PostgreSQL 全量测试 897 passed；Ruff、Mypy（165 个文件）、包构建及差异检查通过。本轮只缩减文档、注释与测试，无新增运行机制。

## 2026-10-13 — 清理既有测试

按用户要求审查旧测试，删除 7 个完整用例、1 个重复参数及未使用的 `RecordingJudge`：基础模型解析、限流/重启、空修订、共享 Store 和工具调用的重复覆盖。
保留真实 SDK、完整重启恢复、修订结果重放、双 Store 并发及 Gateway/Client E2E；严格 schema 与不同失败路径继续各自验证。

本轮 PostgreSQL 全量测试 889 passed；Ruff、Mypy、包构建和差异检查通过。只清理测试，无新增生产机制需要消融。

## 2026-10-13 — 全量测试必要性审查

按用户要求逐项审查 71 个测试文件、556 个函数、889 个参数实例；每项记录触发条件、生产入口和保留/删除理由。
删除 160 个原实例，既有边界断言展开增加 1 个实例，最终 510 个函数、730 个实例；运维脚本、告警与渲染验证也逐项审查。
删除原子提交已排除的残缺状态、无实际消费者的并发调用、不可变归档身份篡改及重复矩阵；断线、超时、事务失败、重启重放等真实风险保留。
有效恢复测试的夹具改用真实 REVISION、PreparedDelivery 和已接收结果，不再借用当前 worker 无法产出的 INITIAL 成功状态。
本轮取代先前保留 retired 缺失回执用例的决定：本地写入证据与回执原子提交，单独删除回执没有当前生产入口。
完整逐项审查表与原始/最终收集清单保存于本地忽略目录 `.cache/2026-10-12/test-necessity/`，不增加仓库维护文件。
本轮 PostgreSQL 全量测试 730 passed、无跳过；Ruff、Mypy（165 个文件）、包构建与差异检查通过。未新增生产机制，无新增消融项。

## 2026-10-13 — 交叉复查后的覆盖修正

复查修正上轮 INITIAL 只能首次执行的判断：完成 OPEN PR 后取消并重试，可以保留 attempt/delivery 再派 INITIAL；新 worker 从 seed A 检出已有后代 H，可以返回 OpenedDelivery。补回回执 wire、重试期间合并后的成功/失败结果，以及重新绑定前合并时禁止接管归档的代表；两种恢复场景共用首次 INITIAL 完成夹具。GraphQL 按 thread ID 读取也可观察仓库改名，增加一个真实身份漂移参数。
删除三个 attempt 替换参数、head-only foreign/lineage 参数、终端 cleanup/nonce 变造断言及不受支持的旧 schema 迁移测试。真实 webhook 并发 receive 保留；单 consumer claim、串行重启 settle 与 blocked 队首按实际入口验证，后者并入既有 poison/retry 用例。issue 争锁改用 webhook trigger 与独立 CLI retry 两入口。
ProjectSync 用真实取消验证版本/read/backoff/rollback/activation；不完整末页使用不可见 item，独立 job 失败使用 ACTIVE+MERGED 的 ARCHIVE Failed→retry。真实风险继续覆盖，虚构两个同步实例和旧 Flow 迟到 SUCCEEDED 删除。
本轮删 8 个不可达实例，补回或替换 6 个真实路径实例，最终 510 个函数、728 个实例；原 889 实例最终删除 162 个，既有边界断言展开增加 1 个。15 项复查问题已落实，5 个外部契约缺充分依据的函数保留；最终映射及逐项理由在本地忽略目录 `.cache/2026-10-12/test-necessity/final-audit.csv`。
PostgreSQL 全量 728 passed、无跳过；Ruff、Mypy（165 个文件）、sdist/wheel 构建和差异检查通过。仅修正测试，没有新增生产机制需要消融。

## 2026-10-13 — 按使用概率与成本整理导入

按用户要求，低成本或通常会使用的依赖直接导入：归档处理、API 默认 runtime 和 Judge 子进程依赖移到模块级；API 配置仍到创建 context 时实例化，Judge 保留 stdout→stderr 的输出隔离。CLI 直接加载 uvicorn、领域类型和离线 GitHub 资源，drain 复用既有 `store.stops`。
测试的 199 处函数内导入全部提升并合并重复项；TYPE_CHECKING 与子解释器字符串不动，非导入 AST、断言及 728 个收集用例保持一致。生产与测试共消除 209 处函数内导入，保留 28 处有实际循环或 CLI/worker 角色隔离的入口；逐处依据和冷导入测量保存于 `.cache/2026-10-12/lazy-imports/`。
复用现有恢复、SDK、进程与边界测试，不增加导入计数或实现镜像测试；无新增缓存、代理或包装层，无新增生产机制需要消融。
PostgreSQL 全量 728 passed、无跳过；Ruff、Mypy（165 个文件）、sdist/wheel 构建与差异检查通过，独立数据库 schema 的 drain/undo 命令验证通过。
