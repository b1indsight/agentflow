# Changelog

## Unreleased

- M2 F4：类型化线程/comment 快照、独立 cursor 分页和共享请求/字节预算；原 review 线程合并回复
  addressed/deferred/可关联问题，PR conversation 说明引用精确来源及已核验提交。
- 先确认回复，再检查完整人类反馈覆盖、本地版本与当前 PR/head，resolve 后复读；新评论和编辑
  幂等入下一批，远端查询落后于同秒 webhook 时不覆盖新版本。addressed 依据与远端 resolved 分开持久保存。
- 线程回复和 resolve 复用持久 effect ledger，响应丢失、重启、取消和数据库提交失败不重复写入；
  权限/映射问题保留诊断，暂停 re-review。旧 pending revision plans 须先核对，wire protocol 保持 v7。
- F4 自动化使用真实 PostgreSQL 与 GitHub 替身；真实 GitHub App/Codex/bub 完整矩阵仍待验收。

- 修复已 push 修订遇到通知永久拒绝后无法恢复的问题：保留核验结果与成功回执，显式 retry 仅补发
  被明确拒绝的通知，不重跑修订或增加轮数；状态不明的通知仍禁止盲目重发。
- 修订准备阶段的 PR/ref/Issue GitHub 错误进入持久失败及显式重试路径；总时限触发 VCS 取消时
  回收并等待整个子进程组退出，避免与后续修订重叠。
- M2 F3：冻结修订的 Flow/attempt/batch、PR/branch/head 与 session generation，每轮签发临时单仓库
  push capability；revision prompt、resume 环境与初始 MCP broker 分离。
- Git/jj 修订独立核验 branch、clean tree、本地/远端 SHA；control 验证原 open PR、仓库与 fast-forward
  lineage，原子提交 session、delivery head、精确反馈与 job 终态。no-op、deferred/clarification 不触发 re-review。
- session 缺失或明确的执行前拒绝允许一次 start；journal 持久记录阶段和调用，超时/不确定执行不重跑。
  Started 与修订额度同事务计数；失败保留 worker binding，结果重放和 fallback 不重复计数。
- 协议升级到 v7，旧未确认结果和已开始但缺冻结 assignment 的修订须先核对；同步 serve/worker 升级。
  完整 PostgreSQL/Git/jj 自动化已覆盖；真实 GitHub/Codex 多轮 smoke 与 F4 thread resolve 仍待验收。
- M2 F2：bub 0.4.1 受控单次调用、无工具/插件/共享会话，配置与凭据只进入 worker 子进程；
  timeout、输出上限、严格 verdict/envelope 与失败分类均有自动化覆盖。
- control 冻结有界 Issue/PR/patch/讨论快照，绑定 head/base SHA 与 context digest；缺失/截断保守 wait。
- answer 直接发布已校验答案及来源，取消、旧 attempt 和变化的 PR 在发送前拦截；持久回执后精确消费，
  不再创建 ANSWER worker job 或二次调用模型。reason/open_points 和模型/adapter 信息随批次持久保存。
- 修复上下文冻结前重启被误判为旧任务，以及 Judge 输出超限后管道阻塞导致回收挂起的问题。
- 无效决策引用或摘要使 Flow 进入可重试失败状态；head/base 变化后 retry 重建上下文并保留旧批次审计。
- 上下文和 capability 准备期间续租，冻结与派发前校验任务所有权，防止过期分配派发或覆盖新任务。
- 协议升级至 v6：serve/worker 同步升级，旧 ANSWER 任务和未应用旧协议结果须先处理；真实 provider
  的语义 smoke 尚未执行，假模型端点验证的是 adapter 契约和控制流。
- M2 F1：反馈版本、settle deadline 和冻结批次持久化；事件交接、批次任务派生及结果消费原子提交。
- 执行中的新评论/编辑保留到下一批；wait 等待新反馈，Request Changes 经授权与 grace 合批后直达修订。
- 修订/答复只确认明确的 item/version，取消和 retry 保持批次所有权，addressed 不再冒充远端 resolved。
- 协议升级至 v5：serve/worker 同步升级；旧在途及仍可重试的失败 review job、未确认结果须先处理。
- 修复恢复超时留下排队任务、retry 选中历史失败，以及升级漏查可重试旧任务的问题。
- ANSWER 保留未决问题；approval/删除不唤醒 waiting；空 review 正文不再阻塞修订完成。
- 同秒乱序编辑持久暂存并按前序回放。
- 移除评论中 `@bot` 直达修订的规则；普通文本和代码示例统一进入 Judge，仅授权的 Request Changes 绕过 Judge。
