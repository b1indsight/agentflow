# 将项目名从 flowtool 改为 agentflow

状态：已在本地实施，未部署。

## 初始方案

1. 将 Python 发行包、命令和插件入口组统一为 `agentflow`，环境变量前缀改为 `AGENTFLOW_`，展示名称使用 `AgentFlow`。同步更新锁文件、帮助、界面、文档和测试。旧 CLI、环境变量和插件入口组不设别名，调用方需同步更新。
2. 同步修改仓库里的 CI、镜像引用、Compose、systemd、Kubernetes 和监控示例。核对生产者与消费者引用一致；不发布镜像、不修改线上资源。升级说明需明确如何复用原有数据库、卷和凭据，避免改名后连接空数据库或启动第二套 worker。
3. 按用途核对旧名称。保留跨版本必须一致的持久化标识（如 PostgreSQL 锁键、已有 schema 标识和 GitHub 评论去重标记）。`FlowToolRequest` 等表示 `flow_tool` 操作，不属于项目名，不改协议或类型名。

不做全局盲替换：改锁键会让新旧进程无法互斥；改去重标记可能导致已有评论被再次发送。本次不迁移历史数据，不改外部仓库或服务。

验收：构建并安装 wheel，执行 `agentflow --help`；验证新环境变量和插件入口；运行相关测试，渲染部署清单并检查引用；逐项核对剩余的旧名称。使用旧数据的兼容性检查在临时数据库中进行。本次不引入新算法，不需消融实验。

## 变更记录

按确认范围完成更名。旧配置键不再参与配置解析；为防止升级时残留的凭据泄露，子进程环境仍过滤旧前缀。保留 worker 握手头、已有 worktree 的 Git hook 配置、事务锁键和 GitHub 评论标记。现有安装的切换顺序见[部署说明](../deployment/README.md#从-flowtool-升级)。

使用临时虚拟环境从 wheel 安装，验证了新 CLI、配置前缀和插件入口，旧入口不生效。pytest 分批验证，恢复连接用例使用临时 TCP PostgreSQL；其余数据库用例使用临时 Unix socket PostgreSQL。Ruff、mypy、Prometheus 规则测试和两套 Kubernetes 模板渲染检查通过，未运行 Docker 容器或部署到集群。
