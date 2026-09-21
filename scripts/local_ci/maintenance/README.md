# 运行维护

`maintenance/` 负责独立健康采集与本地结果保留；Cloudflare 负责外部监测和 Gitee Issue 告警。恢复动作由服务器 Worker、执行器和 systemd 完成。详细规则、升级顺序和演练见 [RECOVERY.md](RECOVERY.md)。

- `health.py` 每约五分钟独立采集 Worker 心跳、任务有效进展、恢复预算、容器真实运行/退出/OOM 状态、systemd 状态及磁盘，发布 `worker-health.json`。查询失败保留未知，正常完成的 oneshot 不当作停止的常驻服务。
- `cloudflare/worker.mjs` 每五分钟读取快照，区分数据不可读、快照过期、服务、任务与交付故障；新鲜且匹配的正常证据才能确认恢复。Cloudflare 不连接服务器执行恢复。
- `retention.py` 每天清理已发布满 30 天的本地大日志和证据，保留结果摘要；准备中、运行中和待上传任务不清理。`retention_until` 可延长指定 run 的本地保留期。

不再安装或读取同机 watchdog。旧 Gitee 文件和历史 Issue 保留；安装器与控制更新器共用精确 unit 清理，停用旧 watchdog service/timer。服务器整体失联由 Cloudflare 根据快照过期发现；不能仅凭 Gitee 读取失败断定服务器宕机。

公开快照按允许字段投影，包含状态、时间和资源统计；路径、凭据、完整 Codex session 与原始异常留在本机。公开异常与恢复事件每任务最多 20 条、全局最多 100 条，并仅保留近 7 天。近期已结束任务用于证明故障是否恢复，防止离开活动列表就被误判为健康。缺字段显示“未上报”。

Codex 的连接、认证和限流分类仅使用 CLI 顶层错误，不从工具或测试日志关键词推断。真实测试失败按 fail 封存和发布；重试不会将已确定的失败重新运行到通过。

结果和所选文件保存在 Gitee `runs/pr/...` 或 `runs/push/...`。本地清理不改远端结果；磁盘不足暂停新任务，但已封存结果仍优先补传。补传以 task_id + run_id 定位，不借用最新运行的身份。

可运行 `python3 scripts/local_ci/maintenance/health.py --config <配置>` 预览，`--publish` 发布；retention 默认预览，加 `--apply` 清理。Dashboard 保留 Gitee 主读、Cloudflare 缓存备用及限流退避，读取页面不写 KV。
