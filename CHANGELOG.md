# Changelog

## Unreleased

- M2 F1：反馈版本、settle deadline 和冻结批次持久化；事件交接、批次任务派生及结果消费原子提交。
- 执行中的新评论/编辑保留到下一批；wait 等待新反馈，强信号经授权与 grace 合批后直达修订。
- 修订/答复只确认明确的 item/version，取消和 retry 保持批次所有权，addressed 不再冒充远端 resolved。
- 协议升级至 v5：serve/worker 同步升级；旧在途 review job 和未确认结果须先排空或核对。
