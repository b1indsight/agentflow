# 统一执行权限模型：路径级文件权限与网络策略

**Stage:** Planning — 待用户批准；本 PR 仅含计划，产品代码和测试尚未修改。

**PR:** [#129](https://github.com/b1indsight/agentflow/pull/129)（沿用同一 draft PR 实现，不先合并计划）。

**Bookmark:** `feat/unified-execution-permissions`

**基线:** `origin/main` / [52cbbced](https://github.com/b1indsight/agentflow/commit/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2)，2026-11-06。

**参考源码:** OpenAI Codex [55897425](https://github.com/openai/codex/commit/55897425e839d943a744c658701445ff732568a0)。

按用户要求整体重写：旧三布尔方案及其修订正文已删除，本页是唯一当前计划。

## 1. 实施计划

### 目标与交付边界

第一版直接实现统一 profile、路径级文件规则、网络目标规则、输入校验和确定性的策略判定，
不再把路径权限和网络策略推迟到后续。继续保留用户此前的要求：**默认全部允许，暂不做实际限制**。

| 层次 | 本 PR 批准后交付 | 本 PR 不交付 |
|---|---|---|
| 策略模型 | 不可变文件/网络策略，统一 `PermissionProfile`，默认 `FULL_ACCESS` | 三个独立布尔开关、互相覆盖的权限来源 |
| 策略判定 | 按路径计算 read/write/deny；按 TCP 主机与端口计算 allow/deny | 拦截进程实际文件操作或网络连接 |
| 运行接入 | runtime 显式传入全允许 profile；两个 adapter 校验可支持的 profile | 把受限策略映射成 CLI sandbox 参数、OS 规则或代理 |
| 配置入口 | 保持当前部署变量 | 受限 profile 的 env/CLI/任务配置入口、审批 UI、热更新 |

因此“实现路径级文件权限和网络策略”在本阶段指可调用、可测试的规则模型与判定器；
**不能将它对一个路径或目标返回的 allow/deny 当成已生效的系统权限。**
真实执行仍按当前账户权限运行，不增加 OS 限制，也不授予超出 OS 账户的权限。
若希望这次同时启用实际限制，需要扩大实施范围并重新确认，不由本计划隐含开启。

### 当前行为与 Codex 对照

agentflow 当前的 Codex start/resume 都使用 `danger-full-access`；Amp 使用 local executor。
两个 adapter 保留 trusted-host 检查，settings 要求声明外部隔离，runtime 拒绝控制面环境凭据。
这些现有保护与新 profile 分开，不从 trusted-host 布尔值派生文件或网络权限。[^af-launch][^af-config]

| Codex 的实现 | 本方案采用或缩减的部分 |
|---|---|
| `PermissionProfile` 区分 Managed / Disabled / External；Disabled 投影为文件不受限、网络 Enabled | 采用一个组合 profile，两个子策略均 unrestricted 时对应当前 full-access；暂不实现 Managed/External 状态，避免声称已有执行后端[^cx-profile] |
| 文件条目 read/write/deny；write 包含 read；最长路径优先，同深度 deny > write > read | 采用这些规则；路径只支持 worker 的 workspace 相对路径和 POSIX 绝对路径[^cx-path] |
| 网络开关与域名/socket 代理配置分离；声明网络权限不会自行启动代理 | 本步实现 TCP 主机/端口 allow/deny 判定；不启动代理，也不设置代理环境变量[^cx-network] |
| sandbox manager 选择后端并转换启动请求 | 本步不复制 backend manager/registry，只检查当前 adapter 能接受的 profile[^cx-manager] |

Codex 的 domain allowlist/deny 优先规则是参考；本方案增加显式目标端口，采用更小的通配语法，
不宣称兼容 Codex 配置格式或复刻其全部策略。Codex 的 Disabled 通常不使用外层 sandbox，
但独立的 managed-network 要求仍会影响后端选择；本方案没有该独立网络执行机制。[^cx-network][^cx-manager]

### 模型：字段、默认值与接口

新增 `app/worker/execution/permissions.py`，仅使用标准库。数据对象统一使用
`@dataclass(frozen=True, slots=True)`，集合使用 tuple；不导入 adapter/runtime/settings，
不增加序列化格式、profile 名称注册表或兼容旧三布尔模型的 shim。

| 类型 | 字段及语义 |
|---|---|
| `FileAccess` | `Literal["read", "write", "deny"]`；write 包含 read，不支持 write-only |
| `PathRule` | `base: Literal["workspace", "absolute"]` 指定锚点；`path: PurePosixPath` 指定路径；`access: FileAccess` 指定读写级别 |
| `UnrestrictedFileSystem` | 无字段；合法路径查询均返回 write |
| `RestrictedFileSystem` | `rules: tuple[PathRule, ...] = ()`；无匹配条目返回 deny |
| `FileSystemPolicy` | `UnrestrictedFileSystem \| RestrictedFileSystem` |
| `TcpRule` | `host: str` 是规范主机、IP 或 `*.suffix`；`port: int` 是精确目标端口；`action: Literal["allow", "deny"]` 是规则结果 |
| `UnrestrictedNetwork` | 无字段；合法 TCP 目标查询均返回允许 |
| `RestrictedNetwork` | `rules: tuple[TcpRule, ...] = ()`；默认拒绝，匹配 deny 优先于任何 allow |
| `NetworkPolicy` | `UnrestrictedNetwork \| RestrictedNetwork` |
| `PermissionProfile` | `file_system: FileSystemPolicy = UnrestrictedFileSystem()`；`network: NetworkPolicy = UnrestrictedNetwork()` |

`FULL_ACCESS = PermissionProfile()` 是唯一默认 profile；`is_unrestricted` 是由两个子策略的
类型计算的属性，不另存 mode/full-access 布尔状态。空 restricted 策略表示该判定域全部拒绝，
不能与 unrestricted 混用，也不能因为“规则为空”被解释为全部允许。

公共接口及用途：

| 签名 | 用途 |
|---|---|
| `PathRule.__post_init__(self) -> None` | 校验锚点、访问级别、路径形式和非法路径组件 |
| `TcpRule.__post_init__(self) -> None` | 校验规则主机格式、端口和 action；规则要求规范形式，不隐式改写 |
| `RestrictedFileSystem.__post_init__(self) -> None` / `RestrictedNetwork.__post_init__(self) -> None` | 保证规则集合是对应规则的 tuple，防止 frozen 对象包住可变列表 |
| `PermissionProfile.is_unrestricted(self) -> bool`（property） | 两个子策略均 unrestricted 才返回 true |
| `resolve_file_access(policy: FileSystemPolicy, path: PurePosixPath, *, workspace: PurePosixPath) -> FileAccess` | 纯词法路径判定；workspace 由实际执行 worker 提供 |
| `allows_tcp(policy: NetworkPolicy, host: str, port: int) -> bool` | 规范化查询主机后匹配；不解析 DNS，不建立连接 |
| `require_supported_permissions(profile: PermissionProfile, *, agent: Literal["Codex", "Amp"]) -> None` | 当前 adapter 只接受 unrestricted；其他 profile 抛 `UnsupportedPermissionProfile` |

规则构造或查询格式非法时抛 `PermissionPolicyError(ValueError)`；不支持把合法策略用于执行时抛
`UnsupportedPermissionProfile(RuntimeError)`。两者都不继承 `SessionUnavailable`，不触发恢复重放。
错误说明问题维度，不输出 prompt、完整策略、环境变量值或凭据。[^af-replay]

### 文件权限语义

1. **路径空间归 worker 所有。** `workspace` 是调用方在执行节点提供的绝对 repo/cwd。
   `base="workspace"` 的 path 必须相对；`.` 表示工作区根。`base="absolute"` 必须是绝对路径。
   不在 control 节点解析路径，也不把 `worker_root` 默认当成可写范围。
2. **查询须明确。** 查询路径和 workspace 都必须为 POSIX 绝对路径；拒绝 NUL、`..` 组件和
   双斜线根 `//`。不展开 `~`、环境变量或 glob；路径中的 `~` 只作为普通名字。
   PurePosixPath 对 `.` 和重复分隔符的词法规范化可用，不调用 `resolve()`、stat 或文件扫描。
3. **按组件匹配。** 一条规则覆盖它本身及其后代；`/repo/a` 不匹配 `/repo/abc`。
   最长匹配路径胜出，同深度冲突采用 deny > write > read；规则顺序不影响结果。[^cx-path]
4. **嵌套可以覆盖。** 父路径 deny 允许被更具体的子路径规则覆盖；deny 的最高优先级只用于
   相同具体程度的冲突。需要完全禁止某个目录时，不能再给其后代增加授权规则。
5. **默认值明确。** restricted 无匹配时 deny，write 同时授权 read；unrestricted 不做细分。
   所有策略先验证查询格式，非法输入不因 unrestricted 被当成有效授权。
6. **静态判定边界。** 不存在的文件按同一词法规则评估；不检查 symlink/hardlink、大小写折叠、
   挂载别名、权限位或 TOCTOU。`workspace/link` 指向外部目录时，字符串仍按 workspace 规则匹配。
   大小写不敏感文件系统也可能有别名。因此本函数不能作为真实 I/O 放行检查或 sandbox 证明。
7. **不暗加路径例外。** 暂不实现 glob、home/tmp 特殊根或隐藏的 `.git`/`.jj` 写保护。
   这些文件如需限制，应写成显式规则；现有 coding agent 的本地 VCS 能力保持不变。

示例 workspace=`/srv/w/task-1`：工作区根 write、`secrets` deny、`vendor` read，另给 `/usr` read。
以下都是判定器结果，当前 CLI 不应用这些规则。

| 查询 | 返回 | 原因 |
|---|---|---|
| `/srv/w/task-1/src/a.py` | write | 工作区根规则 |
| `/srv/w/task-1/secrets/key` | deny | 更具体的拒绝规则 |
| `/srv/w/task-1/vendor/lib.py` | read | 更具体的只读规则 |
| `/srv/w/task-1/vendor-other/a` | write | 不属于 vendor 路径组件 |
| `/usr/bin/git` | read | 绝对路径规则 |
| `/etc/passwd` | deny | 无匹配规则 |
| `/srv/w/task-1/../other` | 格式错误 | 拒绝 `..`，不做字符串折叠 |

### 网络策略语义

1. **第一版范围：TCP 出站目标。** 每条规则是 `(host, port, action)`；端口为 1–65535 的
   整数，拒绝 bool、范围和通配端口。UDP/QUIC、监听/bind、Unix socket 不属于此判定器的能力。
2. **主机形式。** 支持 ASCII DNS 名、标准 IPv4/IPv6 字面量，以及 `*.example.com`。
   通配只匹配至少一个子域标签，不含 apex；不支持全局 `*`、`**`、`?`、CIDR、URL、userinfo、
   内嵌端口或 IPv6 zone id。国际化域名使用已有 ASCII/Punycode 写法，不实现 IDNA 转换。
3. **规范化。** 规则主机须小写、无尾点；IP 使用标准库 `ipaddress` 的规范形式，IPv6 无括号。
   查询 DNS 名转小写、允许去掉一个尾点，再校验标签；IP 查询规范化后匹配。
   同一验证器用于 unrestricted/restricted，非法主机或端口抛格式错误。
4. **明确的优先级。** host 和 port 均匹配才算命中；任一 deny 命中即拒绝，否则有 allow 则允许，
   否则拒绝。网络规则不使用文件的“最长路径覆盖”规则；精确 allow 也不能覆盖命中的通配 deny。
   Codex 同样使用域名 deny 优先，本方案的端口约束属于自己的模型。[^cx-network]
5. **不隐含基础设施白名单。** localhost、loopback、私网、模型 API、GitHub 和 MCP 均无特殊放行。
   IP 规则只匹配该 IP，DNS 规则只匹配名称。名称被允许不代表其 DNS 结果是公网 IP；
   判定器不做 DNS 查询、不检查解析后的地址，也不防 DNS rebinding 或 DNS 外泄。
   实际代理/网络执行层必须另外设计解析、IP 固定与连接检查，不能复用一次名称匹配充当安全边界。
   Codex 源码也明确记录了 DNS rebinding 的限制。[^cx-network]

示例规则：允许 `github.com:443`、`*.githubusercontent.com:443`；拒绝
`blocked.githubusercontent.com:443`。

| 查询 | 返回 | 原因 |
|---|---|---|
| `github.com:443` / `GitHub.COM.:443` | allow | 精确匹配 / 查询规范化 |
| `github.com:22` | deny | 端口不同 |
| `api.github.com:443` | deny | 精确规则不包含子域 |
| `raw.githubusercontent.com:443` | allow | 子域规则 |
| `githubusercontent.com:443` | deny | 通配不包含 apex |
| `blocked.githubusercontent.com:443` | deny | deny 覆盖匹配的 allow |
| `127.0.0.1:443` / `host=::1, port=443` | deny | 无显式 IP 规则 |
| `example.com:443` | deny | 无匹配规则 |

`RestrictedNetwork()` 是 TCP 目标判定上的全部拒绝，不代表当前进程已经断网。
`FULL_ACCESS` 对合法文件/TCP 查询均允许，同时保持当前未限制的其他进程能力。

### 实际接线与不支持的执行请求

两个 adapter 增加相同的关键字参数，保留原参数：

```python
def __init__(
    self,
    *,
    trusted_host: bool = False,
    timeout: float = 1200.0,
    permissions: PermissionProfile = FULL_ACCESS,
) -> None: ...
```

构造时调用 `require_supported_permissions` 并保存不可变 profile；runtime 在当前 agent 装配处
显式传入 `FULL_ACCESS`。生命周期内不替换 profile，不把它写进 prompt、环境或持久化 session。
现有构造调用保持可用；`CodingAgent.start/resume` 和 `CodingExecutionContext` 不变。[^af-launch]

| 请求 | 结果 |
|---|---|
| 默认或显式 FULL_ACCESS，满足既有 trusted-host 等条件 | argv/cwd/env/超时/取消行为保持现状，不调用路径或网络判定器拦截执行 |
| FULL_ACCESS，但未满足 trusted-host/隔离声明/secret 检查 | 按现有位置和错误语义拒绝；默认全允许不撤销原部署保护 |
| 合法 restricted profile 交给独立判定器 | 返回路径/目标判定，可用于测试和受信任代码中的策略计算 |
| 任一 restricted 子策略传给 Codex/Amp 构造器 | 抛 UnsupportedPermissionProfile；说明当前无受限执行后端，不创建 coding 子进程 |
| 非法规则或非法查询 | 抛 PermissionPolicyError，不静默忽略、不自动补全成更宽权限 |

即使 restricted 文件规则恰好覆盖 `/` 且全部 write，也不尝试证明它等价于 unrestricted 后放行。
只按当前明确支持的策略类型接受启动。这个构造期拒绝用于防止误用尚未实现的执行能力，
**不会对正常全允许进程增加文件或网络限制**，也不把拒绝包装成可重试的 session 故障。

默认路径保留 Codex start 的 `-s danger-full-access`、resume 的
`-c sandbox_mode="danger-full-access"`，不升级 CLI。Amp 仍使用 local executor。
原生 skills/MCP、自定义环境继承、现有过滤、本轮 GitHub/MCP capability、固定 session/cwd 和
脱敏行为均保持；不恢复旧环境 allowlist 或工具白名单。[^af-launch][^af-env]

### 文件范围与实施顺序

批准后，在同一 bookmark / PR 依次完成：

1. **模型与判定器：** 新增 `app/worker/execution/permissions.py`，实现上述规则校验和两个
   公共判定函数；新增 `tests/test_execution_permissions.py` 验证调用者可观察的策略契约。
2. **执行接线：** 修改 `app/worker/runtime.py`、`app/worker/execution/codex.py`、
   `app/worker/execution/amp_agent.py`，添加默认 profile 和构造期能力检查，更新对应现有测试。
3. **文档：** 更新 `docs/architecture/components.md`、`docs/deployment/README.md` 和本记录，
   明确默认值、可评估与可执行的区别；不把该模型写成已提供的 OS 隔离功能。
4. **验证与交付：** 执行下节检查、消融和代码审查，再按同一 PR 工作流交付。

不改 settings 输入、credentials、executor、领域端口、任务协议、数据库、前端或依赖。
如果接线需要改动以上保留契约，先回到计划说明具体原因；不借此恢复已删除的 sandbox 扩展桩。
无数据迁移、环境变量迁移或受限模式 rollout；回滚只需撤回新增模块和接线。

### 验证与消融

**验收标准：规则确实能独立判定、默认执行行为不变、无法把未实现的限制伪装成有效保护。**

| 测试入口 | 必须证明的行为 |
|---|---|
| `resolve_file_access` | 上述文件示例；空 restricted；write 包含读取；最长组件前缀；相同路径 deny > write > read；更具体规则覆盖父级；重排规则结果不变；非法路径拒绝 |
| `allows_tcp` | 上述网络示例；空 restricted；显式 loopback/IP 允许；大小写/尾点/IPv6 规范化；通配标签边界；deny 优先；端口独立；无隐式 MCP/模型 API 例外；非法 host/port 拒绝 |
| 规则构造与静态边界 | 错误锚点、非 tuple、非法访问级别/网络动作被拒绝；不存在路径可评估；symlink 名称仅作词法匹配；无文件扫描、DNS 或网络 I/O |
| Codex/Amp 现有 driver 测试 | 默认/显式 FULL_ACCESS 的 start/resume argv、cwd、环境、会话、超时/取消契约不变；两种 adapter 的 restricted 文件、网络及组合请求都在构造时拒绝 |
| `tests/test_worker_runtime.py` | 两种后端均收到相同默认全允许策略；trusted-host 与策略无相互推导 |
| 现有 config/凭据/revision 测试 | trusted-host opt-in、隔离声明、secret 检查、原生继承、本轮 capability 和仅 SessionUnavailable 可 replay 的契约保持 |

测试通过公共规则查询与 adapter 构造/start/resume 验证行为，不为每个 dataclass 字段或简单 getter
单独建测试。对格式与匹配边界采用代表性参数化用例，临时消融脚本不提交。

实现后、代码审查前，临时分别去掉组件边界判断、最长路径选择、网络 deny 优先、端口匹配，
以及 adapter 能力检查。对应测试必须失败，恢复后必须通过；这证明这些机制确实承担验收契约。
还要确认 unrestricted 分支不受 restricted 空规则默认拒绝的影响。

检查顺序：相关 permissions/config/runtime/Codex/Amp/凭据/revision focused tests；
`uv run ruff check app tests`；`uv run mypy app tests`；`uv run pytest`（测试 PostgreSQL）；
`uv build --no-sources`；文档/差异检查。全部测试要求仅针对获批后的实现。
本轮仅检查计划文档与源码依据，没有运行产品或 Codex 测试，也没有完成实际隔离验证。

### 取舍、风险与后续边界

- **取代三布尔模型：** 三布尔无法表达工作区读写、密钥目录拒绝或指定服务端口；直接采用
  子策略联合类型，避免下一步整体替换接口。全允许默认只占一种明确的组合。
- **不照搬三个 enforcement 模式：** 本项目尚无自有 backend；Managed/External 现在只会增加
  无法兑现的状态。外部隔离仍是运维声明，不自动检测或证明它真实生效。
- **不忽略 restricted 请求：** 可独立评估的策略不能无提示地进入 full-access 执行。构造期报错
  的代价小于暴露一套表面有效的安全配置；不提供用户可设置却无效果的受限选项。
- **词法规则的局限：** 当前方法不识别 inode、symlink、大小写别名或 DNS 结果。以后即便启用
  OS 后端，也不能先评估一次路径/名称就直接 exec，必须在真实访问边界强制执行并单独验收。
- **覆盖面必须明确：** 本模型的执行对象是 coding adapter。worker/control 网络、executor 的
  clone/VCS 准备和外部 MCP 服务端权限不在其中。构造期拒绝可避免新 adapter 进入任务执行；
  不能据此概括整个 worker 或任意既有任务无副作用。[^af-replay]
- **MCP 不自动受控：** 允许连接 MCP 不限制服务端能力；Codex 的远程 stdio MCP 启动路径也存在
  `sandbox=None`、`enforce_managed_network=false`，不能把 shell sandbox 泛化到整个 CLI/MCP。[^cx-mcp]

本 PR 后置：真实 Seatbelt/Linux sandbox、受控代理与 DNS/IP 检查、CLI 内部工具和本地 MCP 的覆盖
证明、External 模式、权限审批与单次提权、glob/特殊根、UDP/Unix socket/listen、受限部署入口。
**路径级文件规则与 TCP 网络规则的模型、校验、判定不后置。**

审批只需确认本页范围：先实现这些规则能力，默认全允许并保持现有部署保护，实际限制另行开启。
Windows 路径、真实后端能力差异与跨进程覆盖属于后续设计，不在实现过程中自行扩展。

## 2. PR change timeline

### 2026-11-06 — 重写为路径与网络策略

用户要求参考 Codex 的最新理解，删除旧计划并从第一版包含路径级文件权限和网络策略。
本次按要求整体替换旧正文，保留“默认全部允许、暂不实际限制”的前提；采用组合 profile、
显式规则判定和不支持受限执行时的明确错误。此条记录方案变更，不代表已批准实现。
当前只修改计划，仍使用同一文档、bookmark 和 draft PR；不改产品代码、测试或部署。

## 源码依据

[^af-launch]: agentflow [Codex start/resume](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/worker/execution/codex.py#L55-L168)、[Amp local adapter](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/worker/execution/amp_agent.py#L91-L151)、[runtime 装配](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/worker/runtime.py#L125-L149)。
[^af-config]: agentflow [trusted-host 与隔离声明](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/worker/config.py#L30-L47)、[环境 secret 检查](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/worker/runtime.py#L68-L86)。
[^af-env]: agentflow [原生环境继承与本轮凭据](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/worker/execution/credentials.py#L8-L37)。
[^af-replay]: agentflow [revision 准备和仅 SessionUnavailable fallback](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/worker/execution/executor.py#L228-L256)。
[^cx-profile]: Codex [profile 联合类型](https://github.com/openai/codex/blob/55897425e839d943a744c658701445ff732568a0/codex-rs/protocol/src/models.rs#L300-L318)、[Managed/Disabled/External](https://github.com/openai/codex/blob/55897425e839d943a744c658701445ff732568a0/codex-rs/protocol/src/models.rs#L422-L440)、[Disabled 投影](https://github.com/openai/codex/blob/55897425e839d943a744c658701445ff732568a0/codex-rs/protocol/src/models.rs#L669-L715)。
[^cx-path]: Codex [访问级别及冲突优先级](https://github.com/openai/codex/blob/55897425e839d943a744c658701445ff732568a0/codex-rs/protocol/src/permissions.rs#L101-L139)、[executor-owned context](https://github.com/openai/codex/blob/55897425e839d943a744c658701445ff732568a0/codex-rs/protocol/src/permissions.rs#L263-L273)、[最长路径匹配](https://github.com/openai/codex/blob/55897425e839d943a744c658701445ff732568a0/codex-rs/protocol/src/permissions.rs#L979-L1032)。
[^cx-network]: Codex [网络权限不自动启动代理](https://github.com/openai/codex/blob/55897425e839d943a744c658701445ff732568a0/codex-rs/core/src/config/permissions.rs#L144-L155)、[代理规则与安全边界](https://github.com/openai/codex/blob/55897425e839d943a744c658701445ff732568a0/codex-rs/network-proxy/README.md#L246-L278)、[DNS 授权次序测试源码](https://github.com/openai/codex/blob/55897425e839d943a744c658701445ff732568a0/codex-rs/network-proxy/src/host_policy_tests.rs#L1-L48)。
[^cx-manager]: Codex [后端选择与启动请求转换](https://github.com/openai/codex/blob/55897425e839d943a744c658701445ff732568a0/codex-rs/sandboxing/src/manager.rs#L320-L418)、[managed-network 对 sandbox 需求的影响](https://github.com/openai/codex/blob/55897425e839d943a744c658701445ff732568a0/codex-rs/sandboxing/src/policy_transforms.rs#L646-L665)。
[^cx-mcp]: Codex [远程 stdio MCP 启动参数](https://github.com/openai/codex/blob/55897425e839d943a744c658701445ff732568a0/codex-rs/rmcp-client/src/stdio_server_launcher.rs#L931-L947)。
