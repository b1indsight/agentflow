# Codex 与 Amp：Worker 后端差在哪里？

两个后端都能启动编码、按 ID 续聊、[枚举可编码的模型](../../tests/test_worker_model_catalog.py)，并向控制台提供分页历史。差异集中在任务失败后留下什么，以及哪些约束由 CLI 执行。下表对照 AgentFlow 的两个本地适配器和行为测试；[Worker 镜像](../../Dockerfile#L34-L39)固定了 CLI 版本。这里没有用真实模型测量失败率或输出质量。

| 场景 | Codex | Amp | 对任务的影响 |
|---|---|---|---|
| 最终回答或错误中出现凭据 | 原先只替换本轮 GitHub、MCP 令牌；现在也替换子进程环境中已知的 CLI 凭据原值。[结果处理](../../app/worker/execution/codex.py#L296-L314) | 使用[同一组凭据](../../app/worker/execution/credentials.py)，并能处理常见令牌格式、Bearer 和 JSON 转义字符串。[文本脱敏](../../app/worker/execution/amp_agent.py#L59-L87) | **已修复已知凭据原值的差距**，包括没有任务授权上下文的推理调用。[回归测试](../../tests/test_codex_driver.py#L260-L286)覆盖最终消息和失败详情。Codex 的最终输出仍不具备 Amp 的格式识别能力；两端都不能保证识别任意自定义密钥。 |
| 取消运行或空闲超时 | CLI 仍在运行时，先找出它和后代的进程组，再[向这些组发送 SIGKILL](../../app/worker/execution/codex.py#L237-L262)。 | 使用[同一份进程树快照逻辑](../../app/worker/execution/process_groups.py)，但先向 CLI 发送 SIGTERM，给它退出时间，再清理剩余进程组。 | [真实子进程测试](../../tests/test_worker_idle.py#L58-L110)覆盖取消和静默超时：两端都能终止仍可追溯、另建进程组的子进程。快照前已脱离进程树的后台程序仍须依赖外部隔离。 |
| 新会话已初始化，随后静默或管道输出超限 | [失败结果](../../app/worker/execution/codex.py#L264-L276)从已收取的 stdout 读取 `thread.started` 的 ID；首轮没有收到可解析的初始化事件时，ID 仍为空。 | [解析器](../../app/worker/execution/amp_agent.py#L148-L215)保留已经确认的初始化 ID，不要求收到最终事件。 | 两端都能在失败结果中保留历史查询和[原会话纠正](../../app/worker/execution/executor.py#L202-L243)所需的 ID；失败不会变成成功。见[空闲超时](../../tests/test_worker_idle.py#L92-L98)和[两条管道超限](../../tests/test_codex_driver.py#L243-L256)测试。外层总超时和显式取消不经过这条结果路径。 |
| 修订时原会话已丢失 | 仅在没有执行事件、没有最终文本且 CLI 明确拒绝该 ID 时报告 `SessionUnavailable`。[拒绝判定](../../app/worker/execution/codex.py#L300-L309) | 缺少足以确认「执行前拒绝」的证据时返回失败。[不重放测试](../../tests/test_amp_driver.py#L319-L340) | **Amp 的自动恢复范围较窄**。普通修订在 Codex 上可经[执行器重新核验](../../app/worker/execution/executor.py#L354-L379)后新建会话；不能把 Amp 的通用失败也这样处理，否则可能重复已经执行过的操作。 |
| 要求按 JSON Schema 回答 | [传递 `--output-schema`](../../app/worker/execution/codex.py#L195-L207)，由 CLI 接收结构约束。 | [把 Schema 加入提示](../../app/worker/execution/amp_agent.py#L435-L443)，依赖模型遵守。 | **Amp 缺少同等的生成端约束**。指定 Schema 时，两端的[公共接口](../../app/worker/coding_agent.py#L123-L140)都检查合法 JSON；字段是否满足[首轮业务契约](../../app/worker/execution/executor.py#L254-L279)仍由执行器验证。 |
| 任务正文与图片 | 任务正文作为进程参数，图片通过 `--image` 传入。[命令构造](../../app/worker/execution/codex.py#L177-L200) | 任务正文走 stdin，图片落到临时目录，提示模型调用 `view_media`。[图片处理](../../app/worker/execution/amp_agent.py#L440-L443)、[传入正文](../../app/worker/execution/amp_agent.py#L466-L469) | Codex 的长正文受系统参数长度限制，在能查看进程参数的环境中也可能可见；Amp 图片是否被查看取决于工具调用。两端都能接收带图任务。 |

历史来源也不同：[Codex](../../app/worker/execution/codex.py#L338-L359)查找本机活动或归档的 rollout；[Amp](../plan/amp-session-history.md)在 Worker 运行期间记录公开消息，有缺口时尝试原生导出。两者都服务同一[会话分页接口](conversation-api.md)，不宜把存储方式不同记为功能缺失。

**剩余优先项：** Codex 的最终输出仍可补齐对令牌格式和 JSON 转义的识别。Amp 的会话回退应等到有可稳定区分「尚未执行」的 CLI 证据后再做，避免重复操作。
