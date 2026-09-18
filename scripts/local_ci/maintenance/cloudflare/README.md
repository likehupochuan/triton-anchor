# Cloudflare 外部告警与健康缓存

一个定时 Worker、一份 KV、一个 Gitee Secret。即使 CI 服务器停止或无人打开 Dashboard，Cloudflare 仍会每五分钟读取 Gitee 的公开健康快照，并把异常写入健康仓库 Issues。同时保存一份健康展示缓存，供 Dashboard 在 Gitee 读取失败时使用。

源码为 `worker.mjs`，不依赖 npm 包或服务器新增服务。监测对象集中在文件顶部的 `CONFIG`：

- 健康仓库：`likehupochuan/triton-anchor-worker-health`
- Worker：`jiwang-ci-race-1`
- 快照分支：`snapshot/jiwang-ci-race-1`
- 快照文件：`worker-health.json`

Cloudflare 不改写快照或 `watchdog.json`。服务器现有 health/watchdog 保持原有职责。

## 部署

首次启用 Workers 前，Cloudflare 账户邮箱必须已验证，否则上传会返回 `10034`。本目录 `wrangler.jsonc` 已记录当前账户及 `ALERT_STATE` KV 的部署标识（非凭据），后续命令行部署复用这些资源，不重复创建 namespace。

新账户还需在浏览器打开一次 **Workers & Pages**，完成默认 `workers.dev` 子域初始化；否则定时器配置可能返回 `10063`。本 Worker 使用 `workers_dev: true`，开放只读健康缓存地址。

1. 在 Cloudflare 的 **Workers & Pages** 创建 Worker，名称建议 `local-ci-alert`。把 `worker.mjs` 完整内容放入代码编辑器并部署。`GET /health` 提供缓存；其他路径返回 404，告警仍只通过定时入口工作。
2. 在 Worker 的 **Bindings** 中绑定 KV，变量名必须为 **`ALERT_STATE`**。当前账户复用 `wrangler.jsonc` 中已创建的 namespace；仅在其他账户首次部署时新建。
3. 在 Worker 的 **Settings → Variables and Secrets** 添加 **Secret**，名称为 **`GITEE_TOKEN`**。令牌需要能读取、创建、更新健康仓库的 Issues。由维护者直接在 Cloudflare 输入，不放入仓库、Dashboard、聊天或普通明文变量。
4. 最后在 **Settings → Triggers → Cron Triggers** 添加 `*/5 * * * *`。只配置这一条定时器，也不要另部署第二个 Worker 同时维护这些 Issues。
5. 查看 Worker 的执行记录，确认 scheduled 调用成功。定时配置传播可能需要最多 15 分钟。此项目无需接入 GitHub Actions。

后续更新使用本目录配置部署，保留原 KV 绑定、Secret 和定时器。本次从仅定时告警升级时，还需启用 `workers.dev`，并发布 Dashboard，备用读取才会生效。暂停监测时移除该 Cron Trigger，保留 KV 和历史 Issues；已缓存数据会随时间过期。

也可使用官方 Wrangler 命令行部署，不依赖浏览器控制扩展。`wrangler login --device --scopes account:read user:read workers_scripts:write workers_kv:write workers_tail:read` 会显示授权地址和一次性代码，由维护者在自己的浏览器确认。凭据由 Wrangler 管理，不复制进仓库。首次部署先不设置 Cron，绑定 KV、配置 Secret 并验证之后再启用定时器；以后只维护同一份部署配置。

## 告警行为

- 同一轮连续异常合并到一个 Issue，列出故障类别、首次发现及观测时间；异常集合不变时不重复写入。
- 异常集合变化时更新原 Issue；确认原有问题恢复后，正文补充恢复时间和持续时长并关闭。之后新一轮故障再创建新 Issue。
- 心跳快照超过 20 分钟未更新：报告心跳过期、当前服务状态未知，不能仅凭这一点区分停机、断网和采集停止。
- 连续两轮读取失败：报告健康数据读取异常。不可读或过期时保留此前未确认恢复的故障，不把缺数据解释成恢复。
- 新鲜快照中明确的 Worker、Docker、Gitee 访问、Codex 连接/认证/限流/执行异常，以及磁盘、环境、结果上传异常，会列入告警。
- Codex 的启动、空闲或未上报状态不证明模型服务恢复；已有 Codex 告警需等后续实际运行或成功状态确认。旧服务器需更新控制代码，才会上报这些字段。
- Issue 写入失败会在下一轮重试。新建请求响应丢失时先查找本 Worker 的活动告警，避免直接重复创建。KV 是最终一致性存储，本实现用于单 Worker、单定时器，不提供多个实例同时写入时的严格去重。

Gitee 故障同时可能影响健康读取和 Issue 写入；此时 Cloudflare 执行记录会报告失败，待 Gitee 恢复后重试。只接入 Gitee Issues 无法保证在 Gitee 本身不可用时送达告警。

创建或更新 Issue 不保证发送邮件。若需要邮件提醒，维护者还应检查 Gitee 的仓库订阅与个人通知设置，并实际验证送达。本实现没有接入额外邮件或机器人服务。

## Dashboard

发布新的 `dashboard/worker.html` 后，页面每五分钟优先匿名读取 Gitee 健康快照和告警 Issues；读取失败才使用 Cloudflare 缓存。明确遇到 Gitee 限流时，15 分钟冷却期间直接读缓存，之后再尝试 Gitee。两边都失败时保留最后取得的数据并说明读取失败；数据过期不能表示服务器宕机。

当前缓存地址为 `https://local-ci-alert.2272640910.workers.dev/health`，与 `dashboard/health.js` 的 `source.cacheUrl` 保持一致。这个公开接口只读取 KV，支持跨域 GET；不会接收令牌、写入 Issue 或触发 Gitee 请求。首次部署后需等待一次定时采集，缓存尚不存在或 KV 不可读时返回 503。Cloudflare 的 Gitee 写入令牌和内部告警去重状态不会放入公开缓存。

每轮定时采集合并健康快照、watchdog 和最近告警为一个缓存条目，保留原始 `collected_at`、`updated_at`；外层 `updated_at` 只表示缓存刷新时间。某个来源读取失败时保留该部分旧数据，附固定的读取错误说明，不把原始异常信息发布出去。Issue 写入失败仍会刷新缓存，缓存写入失败也不会阻止本轮告警处理。

正常运行时每五分钟各写一次内部告警状态和合并缓存，合计约 576 次 KV 写入/天；页面访问只读缓存。浏览器缓存有效期为 60 秒。告警变化无需触发 GitHub 工作流、Pages 重新发布或 Git 提交。

Issue 正文用 `<!-- local-ci-alert:jiwang-ci-race-1 -->` 标记归属，不能删除这个标记。不要把 Issue 手工关闭当成修复服务；页面也会区分 Issue 状态与实时健康状态。

## 验证

本地运行 `node --test scripts/local_ci/maintenance/cloudflare/worker.test.mjs`，使用内存 KV 和模拟 Gitee API 检查故障、持续去重、恢复及写失败重试，不会向真实仓库写 Issue。

上线验收需确认一次真实 scheduled 调用、故障 Issue、持续故障不重复写入和恢复关闭。可以在单独的测试仓库与 Worker 上使用测试快照；不要修改生产心跳来制造故障。仅通过本地测试不代表已部署或通知已经送达。

参考：[Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/)、[KV 写入限制](https://developers.cloudflare.com/kv/api/write-key-value-pairs/)、[Secrets](https://developers.cloudflare.com/workers/configuration/secrets/)、[Gitee API](https://gitee.com/api/v5/swagger)。
