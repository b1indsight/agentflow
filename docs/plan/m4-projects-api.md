# M4 Feature 8/9：GitHub Projects v2 最小接口与配置

> 状态：用户已批准顺序独立实施并推送；实现与自动化验证完成；等待用户统一 review/合并。
> 规划日期：2026-09-09；基线：`main@origin` / `be65878f`（归档恢复修复 #27 已合并）。
> bookmark：`feat/m4-projects-api`；实现基线：F7 bookmark；API 能力在技术上独立。
> 一 feature = 一 bookmark = 一 PR；按用户授权，无需等待合并，叠在前一项 PR 上实施并推送。

## 1. 目标与认证边界

给 F9 提供可测试的 Projects v2 读写接口。本项默认按组织 Project v2、现有 GitHub App
installation 认证规划；个人 Project 若需要另一类凭据，应另拆认证 feature，不默认增加 PAT。
此默认在阶段总表记录，实际接入目标明确后可在本 PR 内修订计划。

组织 Project 需要 App 的组织级 Projects 读写权限，仓库级权限不能代替；App 需安装到可访问
目标内容的组织。[GitHub 官方权限说明](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions)

## 2. 方案

- 配置可选，未启用时零 Projects 请求。支持明确的 repo→project 配置：installation、
  project node ID、Status 字段 ID、选项 ID 映射；一 repo 在本期最多绑定一个 Project。
  ID 必须从查询校验，不按展示名称猜测；字段/选项更名后 ID 不变可继续使用。
- 最小 port：读取并校验目标字段与选项；按 Issue content node ID 查找 item；
  添加已有 Issue；读取/设置该 item 的 Status。新增 typed Projects 模型和薄 adapter，
  不扩展通用 Flow tool，不把凭据或 Projects API 放到 worker。
- 以原 Issue 为唯一卡片对象；Issue node ID 通过真实查询取得。查找 item 必须处理分页、
  内容不可见和 incomplete；不能把只读第一页未找到当成需要新建。
- add 与设置 Status 是两个独立 mutation，返回分别校验；先 add 取得 item ID，再 set。
  mutation 禁止 SDK 透明自动重试；HTTP 200 带 GraphQL errors/partial data 不能当成功。
- 响应丢失后的核对由 F9 负责，本 adapter 只返回结构化错误和确切远端事实。
  `clientMutationId` 仅用于关联，不把它当服务端幂等保证。
- 提供最小只读 `flowtool project check <repo>`，校验 App 可见目标、字段和映射并显示
  缺项；不创建 Project、字段、选项或测试卡片。

添加 item 后需要单独设置字段，接口及字段 ID 用法见
[GitHub Projects API](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects)。

## 3. 落点与实施顺序

| 落点 | 改动 |
|---|---|
| `app/config.py`、`cli.py` | 可选映射与只读 check |
| 新增 `app/github_projects.py`、Projects typed models | GraphQL 读写与错误映射 |
| `app/ports.py`、`github_client.py` | 现有 App adapter 接线 |
| 新增 `tests/test_github_projects.py`，既有 config/boundary 测试 | 权限、分页和 mutation 契约 |
| `docs/architecture/github-app.md` | 最小权限及配置示例 |

先写 API 契约和配置校验测试，再实现查询/两步 mutation，最后接只读 check；
此 feature 单独合并后可以核验连接，Flow 自动同步等待 F9。

## 4. 验收

- 未配置零调用；有效配置可校验，字段/选项缺失、installation 不匹配明确报错。
- 第二页已有 Issue 不会判断为缺失；partial data、权限错误和不可见内容不返回伪成功。
- add 与 set 分别测试成功/丢响应；没有隐式写重试，token 不出现在 CLI、日志或异常中。
- 不写其他字段，不创建 Project，不改变 worker 协议。
- 自动化使用受控 GraphQL 替身；真实组织 Project 的只读验证与 F9 写入 smoke 分别记录。

## 规模预算与拆分触发点

按相对合并基线的新增行 + 删除行估算：产品 410、测试 440、文档 100，合计约
**950 行**。数字是规划预算，不是已实现统计；共享测试替身、schema、格式调整也计入。
目标 800–1100 行，预计超过 1200 行先缩小/拆出独立可验收 feature，不压缩测试或代码行数凑数。
优先另拆：个人 Project 新认证、创建看板/字段、PR 双卡和双向同步。未完成必需正确性验收时不能仅以行数达标合并。

本次规划仅做文档内容、链接和 diff 检查。实施按“先失败测试→最小实现→针对性验证→完整检查”
推进；完整检查为实际 PostgreSQL 下 pytest、Ruff、Mypy 和 `uv build --no-sources`。


## 实现记录

新增 `ProjectConfig`/typed facts、独立 `ProjectsClient` port 和 `GitHubProjects` adapter；
复用 App 错误映射，所有 Projects 调用关闭 SDK 自动重试。目标和字段各按 node ID 查询，
App JWT 查询实际 repo installation 后再验证组织、可写性与选项集合。
`items_page()` 每次最多 100 条并保留不可见内容事实；读取 item 的字段最多遍历 10 页，
未完成时明确失败。add/set 分别校验 receipt，不把 clientMutationId 视为幂等保证。
只读 CLI、配置错误、第二页匹配、归档事实、半份响应和丢响应均有契约覆盖；真实 App smoke 未验证。

验证：实际 PostgreSQL 完整套件 **657 passed**；Ruff、Mypy（109 个源文件）和 `uv build --no-sources` 通过。

## 归档卡片枚举返修

每页 items 显式请求 `archivedStates: [ARCHIVED, NOT_ARCHIVED]`，避免将已归档卡片误判为不存在。GitHub 默认只返回非归档卡片，见 [官方 GraphQL schema](https://docs.github.com/en/graphql/reference/projects#projectv2)。新增分页契约回归先失败后修复；F9 继续保留人工归档状态，恢复卡片后显式 retry 可复用原卡。

本轮验证：完整 PostgreSQL 685 passed；Ruff、Mypy（111 个源文件）、build 通过。
