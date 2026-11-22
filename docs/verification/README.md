# 本地验证与打包

在仓库根目录运行以下检查。完整 Python 测试需要 Git、jj 和独立的测试 PostgreSQL，数据库用户要能创建和删除 schema；不要使用生产库。恢复工具的测试需要本机 TCP 回环地址，不能只用 Unix socket。下面使用仓库 Compose 的开发数据库。

```bash
uv sync --locked --dev
uv run ruff check app tests
uv run mypy app tests
docker compose up -d postgres
AGENTFLOW_TEST_DATABASE_URL=postgresql://agentflow:agentflow@127.0.0.1:5432/agentflow uv run pytest
```

未设置 `AGENTFLOW_TEST_DATABASE_URL` 时，数据库用例会跳过。本地 pytest 默认运行全量；[CI](../../.github/workflows/ci.yml) 分片运行 pytest，用 testmon 缓存选择受影响的测试。缓存失效或手动触发时运行全量；包边界和已安装 Worker CLI 的检查不依赖 testmon 选例。

## 构建带控制台的发行包

先用 Node 24 构建控制台，再从仓库根目录打包。安装构建好的发行包后，serve 不需要 Node。

```bash
uv run python scripts/build_console.py
uv build --no-sources
uv run agentflow --help
```

前端单测、开发服务器和生产预览见[控制台开发](../../app/controler/web/README.md)。

## 自动化与真实环境验收

数据库测试验证事务、并发和重启恢复；外部 GitHub、编码 CLI 或模型调用使用替身。bub 契约测试连接本机模拟 provider，不验证真实模型或账号权限。部署后的 Issue → PR → 审核 → 合并须单独按[真实闭环验收](m5-live-e2e.md)执行。历史验收结果见[实施记录](../plan/README.md)。
