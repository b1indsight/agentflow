# 真实 smoke fixture

在专用测试仓库创建初始提交和默认分支，预先安装 App，创建 `flow`、`flow:in-review`、
`flow:done` 标签。目标只修改 `smoke/`，不使用生产仓库。

首轮：新增 greet(name)，返回 `Hello, <name>!`，并提供 Python 标准库 unittest。
第一轮人工反馈：去掉 name 首尾空白，增加对应测试。
第二轮人工反馈：空白 name 回退到 `world`，增加对应测试。

每轮等待交付新 SHA 后才发送下一轮反馈，并运行只读 observe 保存各轮 SHA。
人工 review、合并由测试操作者执行；工具不自动发 review、评论或 merge。
