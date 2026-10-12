# GitHub 游标查询的开发状态

反馈读取使用 `fetch_threads`、`fetch_comments`、`fetch_reviews`，结果为
`Batch[T](items, next_cursor)`。`ReviewThread.comments` 的 JSON 从评论数组改为
`{"items": [...], "next_cursor": null}`；持久扫描 JSON 由 control 的 `ThreadScanState` 管理。
这些类型不提供旧形状、REST 页码或混合 cursor 的兼容读取。

首次使用本版本开发时使用新的空开发数据库，随后按现有 Store 启动初始化。
仅数据库内容全部已经采用新评论结构，而需要重新开始反馈扫描时，可在停止 serve 后执行：

```sql
UPDATE feedback_reconcile
SET cursor = NULL, next_at = CURRENT_TIMESTAMP, failures = 0,
    last_error = NULL, scan_incomplete = FALSE, last_success_at = NULL;
```

该操作只重置扫描调度，不删除反馈版本。存在旧评论数组的冻结批次或 result plan 时，
重置 cursor 不会转换它们；使用新的开发数据库。本变更未操作现有开发数据。
