# Worker 原地自更新

```sh
/原安装路径/bin/flowtool worker --update
```

这是一次性命令：自动查询最新已核验发布，在**现有安装位置**更新包本体和所需依赖，核验后退出。
成功也不会启动 Worker；普通 `flowtool worker` 仍启动执行进程。无需用户指定 SHA。
实现入口见 [Worker CLI](../../app/worker/cli.py) 与 [原地更新模块](../../app/worker/self_update.py)。

更新不读取 Worker 配置或凭据，不连接 Controller、Server 或 PostgreSQL，不要求 Server 在线。
Server 与 Worker 的更新互不等待；Server 流程见 [Server 更新](server-update.md)。
没有后台轮询、全局暂停/活动协议、更新安装器、launcher、新环境或 release 目录，也不操作系统服务。

## 首版支持的正式安装

实现只支持一种原地更新合同，不自动选择其他安装器：

- Linux/macOS 等支持本地目录 `flock` 的 POSIX 主机；使用原有、固定路径的 Python 虚拟环境。
- `flowtool` 由 `uv pip install` 从 `git+https://github.com/b1indsight/agentflow@<完整SHA>`
  安装。`uv` 在 PATH 上可用；当前命令按 uv 0.11.3 的参数合同实现。Git、联网及构建所需工具
  必须已存在，不由更新命令安装。
- 安装包的 PEP 610 `direct_url.json` 必须记录固定仓库 HTTPS URL（也接受 `.git` 后缀）、
  `vcs: git` 和 40 位小写不可变 `commit_id`。它属于实际安装包的来源元数据；
  项目固定的 `0.0.1` 和独立缓存标记都不用于版本比较。
- 虚拟环境、site-packages、包文件、来源元数据和入口必须属于运行命令的同一用户、可写，
  且不能允许组或其他用户写入。包与元数据不能使用符号链接或安装在当前环境外；
  `bin/flowtool` 必须直接使用当前环境 Python 的 shebang，入口为 `app.cli:app`。
- 安装的 SHA-256 `RECORD` 必须完整覆盖包、来源元数据和 CLI，且与实际文件匹配；
  发现本地改动、缺失文件或未记录包文件时拒绝覆盖。已有环境及安装器写入的元数据必须可信；
  这些检查不能抵御安装所有者同时篡改代码和所有元数据。

来源不明、只有 wheel/版本号没有 Git provenance、其他仓库、pip/系统包管理器、
只读安装、混合所有权和非直接 shebang 启动方式在不满足上述检查时非零退出，不会声称 already-current。
容器及 uv tool 等外部托管方式未经验证，不将元数据检查通过视为已支持其托管生命周期。
源码运行不调用 `--update`；没有源码/editable 检测或跳过分支，
也不执行源码拉取、检出切换或 `uv sync`。容器和 Kubernetes Worker 继续按原部署方式换镜像。

## 操作流程与安装级互斥

1. 使用原 Worker 的同一登录用户、HOME 和原虚拟环境入口。保留 Worker ID、token、WSS URL、
   `FLOWTOOL_WORKER_ROOT`、journal、仓库及模型认证；不要新建 root 或删除 journal。
2. 按 [取消与停机](../operations/cancel-drain.md) 正常完成既有工作和停止确认，再人工停止
   **共享此安装的全部 Worker**。由服务管理的进程应先暂停自动拉起，避免更新过程中反复启动。
   CLI 不停服务、不强杀进程，也不等待任务或通信状态。
3. 执行原入口的 `worker --update`。它先取得安装目录（`sys.prefix`）的排他锁，普通 Worker CLI
   在读取配置、创建 Runtime 前取得同一目录的共享锁并持有至运行结束。因此，不同 root 的多个
   Worker 可以共用安装运行，但任意一个都会阻止更新；更新也会阻止普通启动和另一更新。
   占用时立即非零退出，不排队等锁。帮助不需要锁或 Worker 凭据。
4. [latest_release()](../../app/releases.py) 读取固定 `b1indsight/agentflow` 的 Images/main/push 成功运行列表，
   直接核验列表中的运行身份和状态，再检查该固定 attempt 的发布步骤及当前 main SHA，返回已核验的 SHA 字符串。
   不重复读取运行详情，也不追踪选中后新发起的 attempt。新目标还须经公开 GitHub compare 核对为已安装提交的后继；
   无法证明前进、降级、分叉或 API 失败都拒绝安装。选中后只安装这个固定 SHA，不再次解析 main。
5. 安装后用新的隔离 Python 进程（`-I`，不继承 PYTHONPATH）核对实际导入路径、RECORD 和目标
   revision，再用新进程执行原 `bin/flowtool worker --help`，最后执行 `uv pip check`。
   CLI 核验要求进程退出码为 0，不匹配帮助中的参数字样或终端样式。
   相同 SHA 也做完整健康核验，不能因上次安装留下的目标元数据就假报成功。
6. 全部通过才输出 JSON：`state` 为 `updated` 或 `already-current`，并包含 `previous_sha`、
   `sha`、原 `package_path` 和 `python`，然后以 0 退出。操作者再通过原服务或入口显式启动 Worker，
   仍走普通注册和断线恢复协议。

目录锁不需要写锁文件，普通启动也可用于只读安装。原有 [WorkerRootLock](../../app/worker/runtime.py) 仍独立保护同一 root
及其身份。更新子进程继承目录锁，避免调用进程中断后安装子进程尚未结束就允许 Worker 启动。
此互斥只覆盖采用本版 CLI 的进程；升级前的旧 Worker、直接调用 Runtime、手工运行包管理器或
其他服务不遵守此锁，必须由操作者先停妥。不得在运行中移动/替换整个虚拟环境目录。

## 实际安装和核验命令

自更新调用以下命令；`PYTHON` 是当前解释器的绝对路径，`SHA` 是本次经发布和前进检查核验的固定值：

```sh
uv --no-config pip install --python "$PYTHON" --no-python-downloads \
  --link-mode copy --reinstall-package flowtool \
  "flowtool @ git+https://github.com/b1indsight/agentflow@$SHA"
uv --no-config pip check --python "$PYTHON" --no-python-downloads
```

`--reinstall-package flowtool` 确保版本号同为 `0.0.1` 时仍替换本体；不使用 `--no-deps`，
由目标提交的包需求解析、安装或调整必要依赖，不删除环境里的额外包。依赖依据包需求解析，
并非复现仓库 `uv.lock` 的完整锁定环境；不会升级 Git/jj/Codex 等外部工具。

执行目录为现有环境；安装/核验子进程清除 `UV_*`、`PIP_*`、`FLOWTOOL_*`、PYTHONPATH、
PYTHONHOME 和 VIRTUAL_ENV 环境覆盖，且使用 `--no-config`，避免重定向目标环境或关闭依赖安装。
首版不接入私有索引/自定义 uv 配置，需此类设置的安装应继续走人工维护流程。
HOME 和已有认证文件保持不变。安装超时为 600 秒，各进程核验超时为 60 秒。

## 失败与恢复边界

发布、前进检查、安装、fresh-verification、cli-verification 或 dependencies 阶段失败均非零退出，
报告阶段、开始时已知的 `installed` 和目标 `target`；来源未确认时 revision 为 `unknown`。
`installed` 是开始时读取的实际版本，**不表示失败后的环境仍是该版本**。为避免泄漏私有索引地址
或构建凭据，CLI 不转发安装器/核验子进程的原始输出。

原地包更新不是原子事务；失败可能已改变本体或部分依赖，不自动回滚，不自动启动 Worker。
保持服务停止，先使用同一个 `PYTHON` 人工检查 `direct_url.json`、实际导入路径和
`uv --no-config pip check --python "$PYTHON" --no-python-downloads`。若包或依赖需修复，
核对仍可信的固定发布 SHA 后，在**原环境**人工运行上面的安装命令并查看诊断，再重新核验 CLI。
相同 SHA 的 `--update` 不重复安装，依赖损坏时会失败，须人工修复；若入口/依赖已坏到无法执行 CLI，
也由同一 Python/uv 修复，不删 root、journal 或认证来绕过问题。

仅曾运行过 PR 草稿的主机，需另按[历史实施记录](../plan/server-auto-update.md)处理草稿
updater/launcher；主线安装不需要这类迁移步骤。

[本地 CLI 测试](../../tests/test_worker_cli.py) 使用临时安装、公开发布/安装边界替身、真实新解释器和只读 `uv pip check`；
不据此宣称真实 GitHub 下载、正式主机原地安装、服务切换或编码任务已经验收。
