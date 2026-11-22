# Worker skill 仓库与 Flow 初始化

阶段：已实现。范围是 worker 同步和首次编码前的选择，不改设置页面。

## 行为

当前控制台把验证过的仓库全名保存在 `SKILL_REPOSITORY`；worker 初始化推理目录，首次编码在另一个已分配的仓库目录中运行。配置来源仍是控制台，不要求每台 worker 再配置一次。

1. Worker 建好推理目录、连接控制器时获取当前配置，在推理目录内 clone 指定仓库。设置成功更新后通知在线 worker 刷新；离线 worker 重连时同步最新设置。清空设置后，新 Flow 不加载共享 skill。
2. 建议从 `.agents/skills/<name>/SKILL.md` 的 frontmatter 读取 `name`、`description`，用可选布尔值 `always_on: true` 表示始终开启，缺省为可选。该字段和布局是拟议的仓库约定。
3. INITIAL 取得项目工作目录后，先在其 `.agents/skills/` 下链接始终开启的 skill。对其余候选，只将名称、描述和任务交给 agent，允许返回空名单。worker 验证返回的名称属于本次候选，创建选中目录的符号链接，才启动编码。没有候选时跳过选择调用。

例如仓库提供常开的 `style`、可选的 `migrations` 和 `web-ui`。数据库任务选中 `migrations` 后，编码工作目录能发现 `style` 和 `migrations`；`web-ui` 不出现。后续 revision 沿用同一组链接。

## 实现顺序与取舍

- `app/controler/console/api.py` 与 worker 连接协议：在设置写入成功后以及 worker 注册时下发当前仓库。读取私有仓库用该仓库自己的短期只读凭据，不复用项目推送凭据。初始化本地目录不依赖控制器数据库。
- `app/worker/runtime.py` 与 `app/worker/execution/workspaces.py`：在已验证的 `inference_path` 下准备新版本，完整取得仓库并读出目录后才供新 Flow 使用。按版本保留仍被 Flow 引用的目标，避免原地刷新改变正在执行的任务。设置同步失败时不把旧仓库误当成新仓库。
- `app/worker/execution/executor.py`：首次编码前完成选择。现有项目 skill 优先；不覆盖同名文件或目录，不接受路径作为选择结果，不沿着项目提供的符号链接向工作区外写入。链接不应出现在交付提交中；槽位复用时只清理 worker 自己创建的链接。
- 全部链接会让可选 skill 对每个 Flow 可见；每个 Flow 单独 clone 则重复网络请求。worker 共用 checkout、Flow 单独建立链接满足这两个约束，代价是更新后可能暂存多个版本。

## 验收

用本地 Git 仓库验证首次同步、在线换仓、断线重连与清空设置；更新过程中已运行 Flow 的链接仍可读。用返回固定名单的 agent 检查首次编码时工作目录中的实际链接，包括全常开、选空、未知名称和同名项目文件。检查 `git status`、提交内容和槽位复用后的目录，确认不提交链接、不残留上一 Flow 的选择。最后运行 pytest、Ruff 和 mypy。

## 变更记录

1. 实现同时支持独立仓库的顶层 `<name>/SKILL.md` 和项目布局 `.agents/skills/<name>/SKILL.md`（后者优先）。worker 注册和设置成功保存后同步；后台 clone 不占用协议接收循环，INITIAL 等待对应的 checkout。读取凭据或 clone 失败会阻止使用该版本的首次编码，不影响其他 Flow 的修订。
2. 使用仓库提交号固定符号链接的目标。本地 Git 与 PostgreSQL 集成测试覆盖同步、刷新和清空；将版本目录替换为反复覆盖的 `current` 后，同一测试读到 `new` 而非旧任务应得的 `base`。移除始终开启的链接步骤后，选择调用看不到 `style`。两项对照均在内存中替换实现后执行 `pytest -q tests/test_workspace_skills.py`，未改测试预期。
3. worker 为链接生成仅列出本次名称的 `.agents/skills/.gitignore`，已有同名忽略文件时不覆盖并报告冲突。槽位复用依照工作区外的安装记录清理。历史 checkout 保留在推理目录，本次未实现自动回收。
4. 选择提示只列出可选 skill 的 `name`、`description` 和可选 `allowed-tools`。工具声明仅供选择参考，不向选择会话授权。常开标记可放在标准 frontmatter 的 `metadata.always_on: "true"`；兼容已有的顶层 `always_on: true`。注册、首次编码、刷新和清空共用远程 Flow 测试流程，直接检查编码工作区中的链接。
