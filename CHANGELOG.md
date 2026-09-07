# Changelog

## Unreleased

- M2 F1：反馈版本、settle deadline 和冻结批次持久化；事件交接、批次任务派生及结果消费原子提交。
- 执行中的新评论/编辑保留到下一批；wait 等待新反馈，强信号经授权与 grace 合批后直达修订。
- 修订/答复只确认明确的 item/version，取消和 retry 保持批次所有权，addressed 不再冒充远端 resolved。
- 协议升级至 v5：serve/worker 同步升级；旧在途及仍可重试的失败 review job、未确认结果须先处理。
- 修复恢复超时留下排队任务、retry 选中历史失败，以及升级漏查可重试旧任务的问题。
- ANSWER 保留未决问题；approval/删除不唤醒 waiting；空 review 正文不再阻塞修订完成。
- 同秒乱序编辑持久暂存并按前序回放；命令识别正确处理不同长度及带尾随文本的代码围栏。
