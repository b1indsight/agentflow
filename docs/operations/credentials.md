# 凭据轮换

先记录 secret 版本引用和受影响角色，预留停机与 GitHub 重投窗口。新 secret 通过受限文件/Secret
挂载，不放 PR、日志、命令行或截图；备份 HOME 也视为敏感。修改 env 不会热更新正在运行的进程。
轮换不应同时更改 worker ID/root；先在 fixture 验证新凭据再撤销旧凭据。

| 凭据 | 顺序与验证 |
| --- | --- |
| GitHub App 私钥 | 在 GitHub 创建新 key；暂停派发、停 serve，替换仅 control 可读的 key，重启并验证 installation API；确认无旧实例后撤销旧 key |
| Webhook secret | 暂停入口；同步 GitHub App 与 serve 的新 secret 并重启，再开入口；在 GitHub 检查失败 delivery 后重投。应用不接受双 secret |
| Worker token | drain/停对应 worker；停 serve 更新 token 映射，更新 worker secret；先 serve 后固定身份 worker，确认 WSS 注册再解除 drain；其他 worker 会暂时断连 |
| 模型认证 | 等该 worker 执行收敛后停机，更新 CODEX_API_KEY 或该 worker HOME 的受限认证；用测试请求验证后撤销旧 key，不复制 control 的 HOME |
| Judge/模型 provider | 更新对应 worker 的 provider secret，重启并在 fixture 验证；不将 judge 凭据注入 serve |
| Tunnel 凭据 | 仅更新 cloudflared 部署 Secret/私密文件并重启 connector，验证 webhook 精确路由与 catch-all；按实际 Cloudflare 账号流程撤销旧凭据 |
| Metrics token | 同步 serve secret 与 Prometheus credentials_file，重启/重载各自组件；401 期间应产生监控失联告警 |
| DB 凭据/内部 TLS | 停写窗口同步使用方与数据库/CA，验证 readyz 与 WSS；不跳过证书或认证校验作为恢复手段 |

K8s Secret 更新后 envFrom 不会刷新旧 Pod；OnDelete 工作负载要显式受控重建。
文件挂载更新也不代表库/客户端已重新加载；统一按停止、替换、重启、验证执行。
镜像和公开 manifest 只记 secret 名/版本，实际密钥由部署者管理；本 PR 不操作真实账号或 DNS。
