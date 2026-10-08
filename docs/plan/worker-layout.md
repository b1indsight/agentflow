# Worker 目录按职责归组

**Stage:** Implemented
**PR:** https://github.com/b1indsight/agentflow/pull/65

## 1. 初始方案

### 动机与目标

用户要求重新组织 app/worker，使层级更直观。当前 13 个文件平铺，执行、通信和状态职责混在一起。
基于已合并的 PR #64，只调整目录、模块名及引用，保持运行行为和类/API 名称不变。

### 目标结构

```text
app/worker/
├── __init__.py
├── runtime.py              # 组装与运行生命周期
├── config.py               # worker 配置
├── health.py               # 本地探针入口
├── execution/
│   ├── __init__.py
│   ├── executor.py         # initial/revision/archive 编排
│   ├── codex.py            # Codex CLI 适配（原 codex_driver.py）
│   ├── prompts.py          # 提示词构建（原 context_builder.py）
│   ├── vcs.py              # 工作区和版本控制验证
│   └── credentials.py      # 单次 Git 凭据环境
├── transport/
│   ├── __init__.py
│   ├── client.py           # control WebSocket 通信与消息分流
│   └── mcp.py              # worker 内 MCP 服务（原 tools.py）
└── state/
    ├── __init__.py
    ├── session.py          # 会话对象与持久化执行状态
    └── history.py          # Codex 历史读取与分页
```

### 依赖与边界

runtime 负责组装 client、executor、session 和 CodexAgent。
client 调用 session 查询状态及历史；executor 使用 session、Codex 接口、MCP 和 VCS。
MCP 是编码工具的通信适配器，沿用现有 VCS 验证与 FlowToolCaller；不在本次拆分其中的业务逻辑。
state 不导入 transport/execution，history 不处理 socket。
这三个目录用于按职责查找代码，不增加新的运行层或强制抽象；现有 worker/control 导入隔离保持有效。

### 实施范围和取舍

移动 9 个模块，重命名 codex_driver→codex、context_builder→prompts、tools→mcp。
新增包 __init__.py 保持轻量，不重导出类，不保留旧路径转发模块。
同步 app/tests/scripts 中的 import、mock 字符串和命令，更新当前架构文档与 changelog。
runtime/config/health 的入口路径保持不变；已有 worker 命令和探针部署无需改变。
历史计划保留原路径作为决策记录，不批量改写。

仅重命名根目录文件无法体现分组；拆成更多单文件目录会增加层级成本，采用三个子目录。
不移动共享 domain/contracts、不改协议、不改持久化格式、不添加兼容代码或新功能。

### 风险与验证

主要风险是动态导入或测试 mock 字符串遗漏；全仓搜索旧模块路径，核对运维恢复和 Kubernetes smoke。
复用现有边界测试、导入/任务/会话/MCP 测试；Ruff、Mypy、完整回归、构建通过后独立审查。
不为纯文件搬移新增测试。没有新增机制，消融不适用；通过前后同一套行为测试验证等价。

### 实施顺序

1. 确认目录方案。
2. 移动文件并一次性更新代码、测试和运维脚本引用。
3. 更新文档，验证旧路径无运行引用、导入边界与构建产物正确。
4. 审查后在同一 PR 提交实现。

## 2. PR 变更时间线

### 1. 2026-10-08 — 按批准结构迁移

9 个模块搬入 execution/transport/state，三个子包仅含说明性 __init__.py。
同步源码、既有测试 import/mock、恢复脚本和 Kubernetes smoke 的模块路径；
当前文档同步目录，历史计划保留原记录。未新增兼容导出、类或测试，协议/持久化/部署入口不变。

验证（2026-10-09）：830 项完整 PostgreSQL 回归通过（56.13s，1 条既有 Starlette 弃用警告），
Ruff、Mypy（159 文件）、sdist/wheel 构建通过。日志沿用本会话缓存目录：
`.cache/2026-10-08/worker-layout-full.log`。
对 9 个搬移模块剔除 import 后比较 AST，与基线完全一致；wheel 含新层级全部 16 个 worker 模块，
没有旧 codex_driver 路径。全仓运行代码、脚本和当前文档无旧模块引用。
本次没有新增机制，消融不适用；相同 830 项测试和 AST 比较验证行为等价。
