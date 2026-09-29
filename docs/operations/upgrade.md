# 停机升级与回滚

部署前记录旧、新 commit/镜像 digest、协议版本、配置引用、worker ID 与 root/HOME/PVC 映射。
同一窗口升级 serve 和所有 worker；协议不匹配应停止，不关闭校验。M5 当前协议 9，
从旧协议升级必须先收敛旧任务/journal，参见 [停止与排空](cancel-drain.md)。

1. 暂停入口，drain 所有 worker；等 Flow 完成归档、terminal ACK 和 stop 确认。记录不能收敛的原因。
2. 完成 [冷备](backup-restore.md)，保留旧镜像/配置。不要把复制 root 当作已验证恢复点。
3. 停旧 serve/worker，证实没有旧进程。使用同版本代码初始化 schema：K8s 使用 schema Job，
   直接进程/Compose 在停写窗口由唯一新 serve 初始化；失败则停在此处，勿启动 workers。
4. 启动新 serve，检查 readyz/指标/DB；启动固定身份 worker，确认注册、journal 恢复和 drain 状态。
5. 在隔离 fixture 完成 smoke 后开入口，按需重投 GitHub delivery；确认状态后逐个 `worker-drain ID --undo`。

systemd 停服务后原子切换 `/opt/flowtool` 到版本目录；保持数据目录不动，更新 unit/env 后 daemon-reload。
Compose 固定 serve/worker digest，停止后更新镜像再启动；保留卷，内部网关需验证 WSS 连通。
K8s 固定 digest、OnDelete；按 [K8s 升级](../deployment/kubernetes.md) 的 drain/scale/schema/逐 Pod 顺序，
不可把 StatefulSet 模板已更新当成 Pod 已更新；实际 imageID 与计划 digest 必须一致。

失败先停新进程并保留数据库、日志和 journal。仅在确认 schema/协议/数据仍向后兼容时切回旧镜像；
不可逆 schema 或已发生远端副作用时不能盲目降级，走前向修复或隔离恢复评估。
回滚 checkpoint 逐项记录（入口、进程、schema、镜像、身份、inbox、效果），不通过重试覆盖未知结果。
当前没有自动 schema rollback、滚动多 serve 或跨 root 身份迁移。
