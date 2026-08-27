# uv_build 迁移计划

> 状态：待批准 · 日期：2026-08-27。
> 本计划只迁移 Python 构建后端，不改变运行时依赖、CLI、源码包名或部署方式。

## 1. 背景与目标

FlowTool 已使用 uv 管理依赖、虚拟环境和常用开发命令，但构建后端仍为 Hatchling。项目当前是
纯 Python 单包应用，没有原生扩展、动态版本、构建脚本或复杂的包文件筛选；因此改用 uv 原生的
`uv_build` 可以统一工具链，同时保持标准 PEP 517 wheel/sdist 输出。

迁移目标：

- 使用 `uv_build` 构建当前顶层 `app` 包；
- 保持发布名 `flowtool` 和 `flowtool = "app.cli:app"` 命令入口不变；
- 保持现有 `uv sync`、`uv run`、测试和静态检查工作流不变；
- 通过实际构建、产物检查和隔离安装证明 wheel/sdist 可用。

本次不包含：把 `app/` 重命名为 `flowtool/`、迁移到 `src/` 布局、发布 PyPI、生成单文件可执行程序，
或引入 Rust。若以后部分模块 RIIR，`uv` 仍可作为构建前端，但届时应另行把后端切换为 Maturin。

## 2. 技术方案

修改 `pyproject.toml`：

1. 将 `[build-system]` 从 `hatchling.build` 改为 `uv_build`，并把构建依赖约束在与当前已验证
   uv 0.11 系列兼容的 `uv_build>=0.11.3,<0.12`。上界避免 uv 的次版本破坏构建可复现性。
2. 删除 Hatchling 专用的 `[tool.hatch.build.targets.wheel]`。
3. 新增 `[tool.uv.build-backend]`，显式设置 `module-name = "app"` 和 `module-root = ""`。
   这是必需的，因为发布名是 `flowtool`，而 Python 包位于仓库根目录的 `app/`，不符合
   `uv_build` 默认的 `src/flowtool/` 自动发现布局。
4. 重新检查 `uv.lock`。构建依赖通常不进入项目运行依赖解析；仅当 uv 判定锁文件元数据确实变化时
   才更新它，不制造无意义的锁文件改动。

迁移不增加 Hatchling 兼容层，也不同时保留两个构建后端。失败应在构建阶段直接暴露，避免产生
看似成功但遗漏模块或命令入口的 wheel。

## 3. 变更边界

计划保留一个独立的计划变更，批准后在其子变更中完成一个内聚的实现单元：

1. **构建后端迁移**：修改 `pyproject.toml`，按需同步 `uv.lock`，构建并验证 sdist/wheel、包内容、
   元数据、CLI 入口和现有质量门禁。配置、验证所需的最小测试资产及由实现得出的计划状态更新
   放在同一变更中，因为它们共同证明一个不可再拆分的构建结果。

不为配置文本增加读取 `pyproject.toml` 后断言字段值的单元测试；那只会复述配置。测试重点放在
安装后行为和最终分发产物上。

## 4. 验证策略

实现阶段按以下顺序验证：

1. 在临时输出目录执行 `uv build --no-sources`，同时构建 sdist 和 wheel，证明标准发布元数据独立于
   本地 source override。
2. 检查 wheel 至少包含完整 `app` 包、`flowtool-*.dist-info` 元数据和 `flowtool` console-script 入口，
   且不包含仓库测试、缓存或本地配置。
3. 从构建出的 wheel 创建临时隔离环境并安装，运行 `flowtool --help`，证明入口不依赖源码 checkout。
4. 执行 `uv lock --check` 与 `uv sync --locked`，证明依赖锁仍一致。
5. 执行现有完整门禁：`uv run pytest`、`uv run ruff check app tests`、`uv run mypy app tests`。

临时构建和安装目录放在系统临时目录，验证后不纳入版本控制。

## 5. 文档影响

- 本计划记录迁移动机、边界、配置选择、验证方式以及未来 RIIR 时切换 Maturin 的边界；实现完成后
  更新本计划状态和实际验证结果。
- `README.md` 的用户命令仍是 `uv sync` / `uv run`，没有提到 Hatchling，因此无需修改。
- `docs/ARCHITECTURE.md` 只把 `pyproject.toml` 描述为“uv 管理”，迁移后仍然成立，因此无需修改。
- 仓库目前没有 changelog、打包指南或构建配置示例，不为本次内部工具链迁移新建这些文档。

若实现验证发现用户命令、产物布局或架构描述发生变化，应在同一实现变更中补充相应当前事实文档，
并在此记录偏差。

## 6. 风险与回退

- **包发现错误**：发布名与模块名不同，若漏配模块路径可能得到空 wheel；通过显式模块配置和产物检查防止。
- **CLI 入口遗漏**：构建成功不代表 console script 正确；通过隔离安装后的 `flowtool --help` 防止。
- **版本漂移**：`uv_build` 跟随 uv 的版本策略；使用有上界的 0.11 版本范围，后续升级作为独立变更验证。
- **未来 Rust 扩展**：`uv_build` 只支持纯 Python；首次引入 PyO3 模块时改用 Maturin，而不是在本次提前增加复杂度。

若迁移无法生成与当前 Hatchling 等价的可安装产物，回退仅需恢复原 `[build-system]` 和
`[tool.hatch.build.targets.wheel]`，不涉及应用代码或数据迁移。
