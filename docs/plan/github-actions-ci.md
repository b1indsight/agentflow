# GitHub Actions CI 计划

> 状态：已实施并验证 · 日期：2026-08-27。
> 首版只自动化 Ruff、Mypy、pytest 与 Python 分发包构建，不包含发布或部署。

## 1. 背景与目标

仓库已有本地验证命令，但还没有 `.github/workflows/`，因此 pull request 和 `main` 上的提交不会自动
获得质量门禁结果。首版 CI 的目标是把当前最关键的四个检查搬到 GitHub Actions：

- `uv run ruff check app tests`：阻止已知静态检查问题进入主分支；
- `uv run mypy app tests`：验证应用与测试代码的静态类型；
- `uv run pytest`：运行现有测试套件；
- `uv build --no-sources`：同时构建 sdist 与 wheel，并验证 `uv_build` 配置可在 CI 环境工作。

本次明确不加入 Ruff 格式检查、覆盖率阈值、跨平台/多 Python 版本矩阵、制品发布、部署、
CodeQL、Dependabot 或分支保护设置。它们有各自的策略与维护成本，后续按实际需要独立增加。

## 2. 技术方案

新增 `.github/workflows/ci.yml`，采用以下边界：

1. 在所有 pull request、推送到 `main` 以及手动触发时运行，保证评审、合并后基线和故障复跑都有入口。
2. 设置 workflow 级 `contents: read` 最小权限；checkout 不保留写入凭据，不读取项目 secret。
3. 对同一 ref 启用 concurrency，并取消已被新提交取代的旧运行，减少无效 runner 消耗。
4. 使用 Ubuntu 托管 runner 和 Python 3.12。3.12 是项目声明的最低版本，也是当前静态配置基线；首版不把
   “支持所有未来 Python 版本”混入四个基础门禁。
5. 使用官方 `actions/checkout` 与 `astral-sh/setup-uv`，都固定到不可变的完整 commit SHA，并在旁注标明
   对应 release。uv 固定为仓库已验证的 0.11.3，启用以 `uv.lock` 为依赖输入的内置缓存。
6. 把 `ruff`、`mypy`、`pytest`、`build` 设为四个并行 job。每个 job 有明确名称和超时；静态检查与
   测试在 `uv sync --locked --dev` 后执行，build 直接用锁定的 uv 构建标准分发包。分开 job 可在 PR 上
   同时显示四个门禁，并避免一个失败遮住其他结果。

不抽取本地 composite action：当前只有四个很短的 job，共享层只包含 checkout/setup，同步抽象会让首版
配置更难阅读，并没有形成需要独立维护的复用边界。

## 3. 变更边界

批准后保留本计划 change 为栈底，并在一个子 change 中完成一个内聚结果：

1. **`ci: add core GitHub Actions checks`**
   - 新增 workflow、最小权限、触发条件、并发取消、超时和固定版本的工具安装；
   - 增加 Ruff、Mypy、pytest、uv build 四个 job；
   - 同步 README 的验证说明、计划索引和本计划实施状态；
   - 本地验证四个底层命令，推送后验证草稿 PR 上的四个 GitHub check。

这些内容共同交付一个可用的 CI 门禁，没有可独立回退的第二个功能边界，因此不人为拆成多个实现 change。

## 4. 验证策略

实施阶段验证：

1. 检查 workflow YAML 的结构、触发条件、权限和 action SHA，确认没有宽权限、可变 action tag 或 secret；
2. 运行 `uv sync --locked --dev`，证明 CI 使用的锁定安装方式可用；
3. 运行 `uv run ruff check app tests`；
4. 运行 `uv run mypy app tests`；
5. 运行 `uv run pytest`，确认实际收集并通过现有测试；
6. 在临时输出目录运行 `uv build --no-sources`，确认同时生成 sdist 与 wheel，且不把构建产物留在工作树；
7. 推送实现后等待草稿 PR 的 `ruff`、`mypy`、`pytest`、`build` 四个 check 全部成功，并核对重复提交会取消旧运行。

CI 不访问 GitHub App、Codex 或 bub，不使用真实凭据，也不启动对外服务。

## 5. 文档影响

- 本计划记录首版范围、技术选择、验证结果和后续扩展边界；实施后更新状态与实际结果；
- `docs/plan/README.md` 增加本计划入口，并同步已经合并计划的分类，使索引保持当前事实；
- `README.md` 的验证基线补充 GitHub Actions 自动门禁及 workflow 链接，但保留现有本地命令作为复现入口；
- 架构组件、公开 API、配置 schema、运行时行为和示例环境变量都不改变，因此
  `docs/architecture/`、`env.example` 无需更新；
- 仓库没有 changelog，本次也不创建发布体系。

## 6. 风险与回退

- **依赖或 action 漂移**：固定 uv 版本和 action 完整 SHA；升级作为可审查的显式改动。
- **重复耗时**：四个 job 会分别准备环境，但能并行并提供独立状态；uv 缓存和 concurrency 控制成本。
- **本地通过、runner 失败**：统一使用 `uv.lock`、Python 3.12 和现有命令，且以 PR 上的真实 checks 作为最终验收。
- **构建被源码覆盖掩盖**：`--no-sources` 禁止本地 source override 影响标准分发包构建。

若 GitHub Actions 环境无法稳定运行，回退只需移除该 workflow 及对应 README 说明，不影响应用代码、
数据库或本地开发命令。

## 7. 实施结果

实现与批准范围一致，没有加入发布、部署、格式检查、覆盖率或版本矩阵。四个检查通过一个
`fail-fast: false` 的静态矩阵生成独立并行 job，共享相同的安全 checkout、uv/Python 安装和缓存配置，
避免复制四份准备步骤；这不改变计划要求的独立检查结果与失败隔离。

本地验证结果：

- workflow 通过 YAML 语法解析，action 固定为 `actions/checkout` v7.0.1 与 `astral-sh/setup-uv`
  v10.0.1 的完整 commit SHA；
- `uv sync --locked --dev`、Ruff 和 Mypy 通过；
- pytest 在本地 Python 3.13.12 环境收集并通过 19 项测试；
- `uv build --no-sources` 在临时目录生成 `flowtool-0.0.1.tar.gz` 与
  `flowtool-0.0.1-py3-none-any.whl`，工作树没有构建产物。

草稿 PR 的首次真实运行（Actions run `33061311196`）在 GitHub 托管的 Python 3.12 runner 上通过
全部四项 checks：Build 11 秒、Ruff 14 秒、pytest 20 秒、Mypy 44 秒。workflow 的 concurrency key
按 PR 编号或 ref 分组，且 `cancel-in-progress: true`，新提交会取消同一分组中已被取代的运行。
