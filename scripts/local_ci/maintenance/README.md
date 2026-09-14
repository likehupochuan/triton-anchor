# 运行维护

`maintenance/` 负责健康采集、异常观察和本地结果保留。Worker 主机上的 health 与 retention 服务由 `prepare/install.py --apply` 一并安装启动。

- `health.py` 每约五分钟独立采集 Worker 心跳、任务状态、容器资源和磁盘情况，将公开快照提交到 Gitee 健康仓库。
- `watchdog.py` 由 health 仓库的 Gitee Go 定时流水线执行，读取所有 Worker 的公开快照，将异常及恢复同步为 Gitee Issues。同一异常持续期间不重复评论；恢复时关闭 Issue；某个 Worker 快照不可读时，保持它已有的 Issue 不变。
- `retention.py` 每天清理已发布满 30 天的本地大日志和证据，保留结果摘要。准备中、运行中和待上传任务不清理；`retention_until` 可延长指定 run 的本地保留期。

公开快照按字段构造，包含状态、时间和资源统计。宿主路径、模型配置、凭据与原始异常留在本机。health 不依赖 Worker 或 Codex 运行；watchdog 不在 Worker 同机运行，因此可检测整机断联。

## Gitee health watchdog 部署

1. health 仓库需开启 Issues，并为流水线凭据授予该仓库的 Issue 读写权限。凭据只放在 Gitee Go 密钥变量 `GITEE_HEALTH_TOKEN` 中。
2. 将 `gitee-watchdog-config.example.json` 复制到 health 仓库受信任的默认分支，填写 health 仓库与全部 Worker ID。
3. 在 health 仓库配置 Gitee Go 定时触发（建议每 5 分钟），使用固定的受信任 control revision 运行：

   ```sh
   python3 scripts/local_ci/maintenance/watchdog.py --config /workspace/gitee-watchdog-config.json --sync-issues
   ```

4. 先手工运行一次该流水线，确认能读取 `snapshot/<worker>/worker-health.json` 并能创建/关闭测试 Issue，再启用定时触发。源完全不可读时命令以非零状态退出，交由 Gitee Go 报告 watchdog 自身故障。

结果和选择上传的文件保存在 Gitee 的 `runs/<task_id>/<run_id>/`。本地清理不改写远端结果。磁盘可用空间不足或结果存储超出配置预算时，Worker 暂停接收新任务。

可单独运行 `python3 scripts/local_ci/maintenance/<入口>.py --config <配置>`。health 加 `--publish` 发布快照；watchdog 加 `--sync-issues` 同步 Issues；retention 默认预览，加 `--apply` 执行清理。各入口均支持 `--help`。
