# Changelog

## Unreleased

- M2 F2：bub 0.4.1 受控单次调用、无工具/插件/共享会话，配置与凭据只进入 worker 子进程；
  timeout、输出上限、严格 verdict/envelope 与失败分类均有自动化覆盖。
- control 冻结有界 Issue/PR/patch/讨论快照，绑定 head/base SHA 与 context digest；缺失/截断保守 wait。
- answer 直接发布已校验答案及来源，取消、旧 attempt 和变化的 PR 在发送前拦截；持久回执后精确消费，
  不再创建 ANSWER worker job 或二次调用模型。reason/open_points 和模型/adapter 信息随批次持久保存。
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
