# Worker Amp CLI 适配

**状态：** 已实现，未部署；PR 与检查状态以 GitHub 为准。
**PR：** [#107](https://github.com/b1indsight/agentflow/pull/107)，目标 `main`。
**基线：** [#103](https://github.com/b1indsight/agentflow/pull/103) 与
[#109](https://github.com/b1indsight/agentflow/pull/109) 均已合并。
**Bookmark：** `feat/worker-amp-cli`。

## 目标与范围

为 worker 增加可选 Amp CLI 后端，沿用现有 `CodingAgent.start/resume` 和
INITIAL → REVISION → ARCHIVE 流程。默认仍为 Codex；一个 worker 固定一个后端，
更换前须收尾存量 Flow。不增加 SDK、插件、远程 executor、模型路由或跨后端会话迁移，
不改 UI、Judge、数据库字段名及 control 的 GitHub 操作权威。

## 实现与关键决定

| 位置 | 职责 |
| --- | --- |
| [`config.py`](../../app/worker/config.py)、[`runtime.py`](../../app/worker/runtime.py) | 选择后端和对应超时，复用现有 executor |
| [`amp_agent.AmpCodingAgent`](../../app/worker/execution/amp_agent.py) | 本地私有 thread 的 start/resume、Schema 提示与结果适配 |
| [`amp_process.py`](../../app/worker/execution/amp_process.py) | 每轮环境/settings、进程监督及输出脱敏 |
| [`AmpHistory`](../../app/worker/state/amp_history.py)、[`WorkerSession`](../../app/worker/state/session.py) | 原生历史导出、公开块映射、缓存与分页 |
| [`Dockerfile`](../../Dockerfile) | 仅 worker 安装固定版本 Amp，serve 不包含 Amp |

- **结果权威不变。** prompt 经 stdin；只接受同 session 的完整 init/result、进程退出 0
  及成功终态。最终文本经脱敏后交给既有 Pydantic、VCS 和交付回执核验。Schema 仅作提示，
  不修复 JSON、不剥 Markdown、不增加二次模型生成或结果提交工具。
- **恢复保守失败。** REVISION 显式继续原 `T-UUID`，禁用旧 INITIAL MCP。
  缺失 thread 的 stderr 不足以证明未执行，故不自动重放；原本无 session 的单次 start
  fallback 保留，不按最近 thread 猜测或跨后端重建。
- **使用原生历史。** 通过公开 `amp threads export` 读取快照，支持外部客户端追加；
  不保存第二套录制历史，不读取未公开存储格式。只返回公开消息和工具块，排除 thinking；
  缓存、导出并发/超时/字节和页面预算有界，追加可续读，已消费前缀被编辑则 cursor 失效。
  control 查询窗口为 30 秒，覆盖 20 秒导出及进程清理。
- **逐轮凭据。** 支持专用 worker HOME 的原生登录或可选 `AMP_API_KEY`；每轮 Git/gh 使用
  assignment 的 GitHub App capability，提交作者沿用配置。不继承个人 GH/control 凭据，
  不修改个人全局设置。INITIAL 仅开放本轮 MCP，冲突的 workspace Amp 设置在执行前拒绝。
- **取消与隔离。** 有界读取 stdout/stderr；先捕获子进程组，再 TERM、等待并升级清理。
  随后沿用 #109：清理成功才报告停止，copy/worktree 原地复用，已知 merge 保留位置供
  ARCHIVE。settings 不代替 OS 隔离，无法保证清理任意脱离进程树的守护进程。
- **脱敏只做必要工作。** 文本检查 JSON 字符串字面量；结构化工具载荷直接遍历并序列化
  一次，保留转义 token 和孤立代理码点处理。resume 复用共享 SessionId 校验，不重复设置
  已由上游保证的长度/条数限制。

## 配置与发布合同

- `FLOWTOOL_CODING_AGENT=amp`；默认 `codex`。
- `FLOWTOOL_AMP_RUN_TIMEOUT_SECONDS=1200`；Amp 固定 `--mode low`。
- worker 镜像固定 Amp `0.0.1792555249-g702cd1`，下载官方 Linux x64 产物并校验 SHA-256；
  具体值以 Dockerfile 为准，不在计划中重复维护。
- 最终合同为 **wire v15 / journal v4 / manifest v1**；serve/worker 固定同一 commit 或镜像。
  已撤销的 ACK 字段、StopCompleted、wire v16 及存储升级不再保留，也不提供兼容转换。
- 部署与原生登录步骤见 [部署文档](../deployment/README.md)；ARCHIVE 不删除远端 Amp thread。

## 验证与证据

| 范围 | 已有结果 |
| --- | --- |
| Adapter、历史和取消 | 覆盖 start/resume、终态拒收、当前凭据、Unicode/转义脱敏、外部追加/编辑、分页及真实子进程超时/取消 |
| 修复回归 | 关键修复均有修复前失败、修复后通过的对照；最近相关 Amp 套件 42 passed |
| 机制消融 | 移除终态判定、REVISION MCP 禁用、子进程组快照或转义字符串检查，均捕获预期回归 |
| 本地全量 | 对齐 main 后 PostgreSQL **942 passed、0 skipped**；Ruff、mypy（205 文件）、文档链接、脚本语法和差异检查通过；此前 wheel/sdist 及 wheel 隔离导入通过 |
| 最新已完成 CI | [测试/静态检查/构建](https://github.com/b1indsight/agentflow/actions/runs/39757664265)中 pytest 为 929 passed、13 skipped；[镜像及 Docker smoke](https://github.com/b1indsight/agentflow/actions/runs/39757664244)、[恢复](https://github.com/b1indsight/agentflow/actions/runs/39757664336)、[Kubernetes](https://github.com/b1indsight/agentflow/actions/runs/39757664518)通过，共 8 项成功、publish 跳过；对应精简前提交 |
| 真实 CLI | 临时 Git 仓库 INITIAL → 同 thread REVISION，提交并推送本地 bare，真实 WorkerMCP 核验通过；control RPC 使用替代，未执行真实 GitHub 写入 |

最近职责收窄/重命名及删除静态测试两次独立门禁均为 **0 findings**，不代表下列既有问题
已解决。本地日志位于 `.amp/in/artifacts/amp-cli-calibration-20261021/`，不随代码发布。
本次对齐 main 的冲突处理精确复用了已通过 CI 的组合树，业务代码和运行配置未变化；
新 head 的检查独立追踪，不能用旧 head 的 CI 状态代替。本次本地日志为 `ready-pytest.log`、
`ready-collect.log` 和 `ready-db-cleanup.log`；测试 schema 已清空，专用 PostgreSQL 已停止。

## 已知问题与验收边界

1. **Medium，继承 #109：** 取消 REVISION 后显式 retry 仍只 lookup occupied 位置，
   没有重新占用已回收的 idle 目录，可能 exit 65。
2. **Low：** INITIAL executor 未向控制面传递 adapter 已保留的启动诊断。
3. **Low，覆盖缺口：** 已知 merge 保留原位置供 ARCHIVE 缺少永久端到端回归。
4. 极深 Unicode 重复编码字符串仍可能触发 Python 递归上限；历史返回
   `history_unreadable`，最终答案仍可能失败。本次不新增通用深度策略。
5. 未完成真实 GitHub App installation-token 写入闭环和 Linux 原生 Amp 模型执行/取消验收；
   CI 镜像 smoke 不代表这些场景通过。原生历史可能滞后，不承诺离线可用，也不允许其他
   客户端并发继续 worker 正在执行的 thread。长期 worker 容器不按单次取消销毁。
6. managed policy、账户插件及服务端 MCP 仍需运维核查，CLI settings 不能充当权限隔离。

取消入口、权限和状态反馈优化由 [#108](https://github.com/b1indsight/agentflow/issues/108) 跟踪。
本次文档精简与 PR 状态调整不包含上述问题的修复或部署。

## 决策摘要

- 2026-10-21：原生导出和原生登录实测后，替换“只录 worker 输出”和“API key 必填”的
  初始假设；选择现有 CLI adapter，未采用 SDK、插件或额外结果工具。
- 2026-10-21：完成后端、历史与镜像适配；按实测收窄缺线程自动恢复及取消回收保证，
  修复 Unicode JSONL、凭据过滤、历史查询窗口和 adapter 诊断。
- 2026-10-22：跟随 #109 最终最小回收方案，确定 wire v15 / journal v4 / manifest v1；
  收窄脱敏职责和重复校验，明确模块/类名，删除容器配置文本断言。
- 2026-10-22：#109 合并后对齐 main；按用户要求将本文件收敛为当前方案、证据和未修项，
  完整调研、初始计划和逐轮变更保留在[精简前记录](https://github.com/b1indsight/agentflow/blob/b14da113d1d64551b00f0dbdcc96b5ff0f20c706/docs/plan/worker-amp-cli.md)。
