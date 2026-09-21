# CI Dashboard

提供三个业务视图：`local-ci.html` 的任务与证据，`index.html` 的全量算子和后端与性能；另有独立的 `worker.html`「Worker 运行状态」页面。
业务视图统一读取 Gateway 短作业生成的 `data/tasks.json`（`triton-anchor-dashboard`）；
`data.js` 将统一结果投影到三个界面，保留选测/未执行原因、审查依据、失败详情、筛选、分页及 CSV/XLSX 下载。

结果与所选文件随同一 Git 提交发布。页面展示所有检查状态、审查结论和文件链接；未选择、未执行和不适用的检查保留原始状态及说明，未选中或超预算的文件保留在主机，并说明省略原因。PR 评论只列出实际执行的检查，并链接回本页面查看完整记录。
全量算子视图只显示真实 full FlagGems 结果；性能读取任务测量和同条件比较，无数据时明确留空。
任务详情分别展示 `environment.variants.base` 与 `candidate` 的 Profile 和已记录的后端开关；
全量算子、后端汇总及性能视图使用 candidate 的 Profile。单环境结果读取 `environment.profile` 或
`generation`；环境信息缺失时显示“未记录”，不根据目标分支或另一侧环境推断。
仓库中的初始 feed 为空，没有展示样例成功数据。
接收器也读取结果仓库 `runs/` 中的历史结果及旧版 `delivery-summary.txt` 对应的真实算子/性能报告。
历史记录只用于展示，不重跑、不回写旧任务的 GitHub 门禁；全量算子和各性能指标分别保留最近一次有效数据，
标注来源提交与测量时间，新任务未选择或尚未完成这些检查时不会清空历史数据。

“未通过”（fail）和“执行错误”（infra_error）分别展示和筛选：前者表示检查或审查明确未通过，
后者表示执行过程出错。算子统计分别计数，后端状态也保留此区别；未知错误不根据非零退出码猜测根因。

任务详情的“阻塞原因”优先逐项展示阻塞 findings 的结论、分析和代码位置，不把失败检查或
审查诊断重复列为缺陷。没有阻塞 finding 时，展示封存结果中的 `blocking_reasons`；
失败报告仍缺少原因时使用任务摘要兜底。检查状态、诊断与证据保留在详情和完整执行报告中。
`limitations` 单列“限制说明”，保留环境、工具、验证或证据发布限制及其对结论的影响。
即使存在明确缺陷或整体检查通过，也展示已记录的限制；展示不改变 CI 状态和最低验证要求。
未提供 `limitations` 的结果兼容原有诊断分类，结合检查、审查及任务摘要展示原因。
缺少审查不等于审查发现代码问题，普通超时也不自动归因于网络。
页面测试：`node --test scripts/local_ci/tests/dashboard.test.cjs`。
“非阻塞发现”与其他发现项显示结果中记录的风险等级（严重/高/中/低/提示）；未提供等级显示“未标注”，展示不改变阻塞判定。

```bash
python3 -m http.server 8000 --directory dashboard --bind 127.0.0.1
```

Gateway 发布时将 `_site/data/tasks.json` 复制到页面的 `data/`。
`worker.html` 独立加载 `health.js`，展示运行概览、当前异常、服务与资源、当前任务和最近异常记录；业务页面只保留导航入口，不加载健康数据。Worker 页面每五分钟匿名读取 Gitee 文件 API 的 `snapshot/<worker>/worker-health.json`，不等待任务发布，不增加 Actions 定时任务。健康仓库、Worker ID 和 20 分钟过期阈值集中在 `health.js` 的 `source` 中；普通 raw URL 没有浏览器跨域许可，不能替代文件 API。
Gitee 读取失败时，页面从 `source.cacheUrl` 读取 Cloudflare 定时保存的合并缓存，只补充读取失败的部分。遇到 Gitee 429 或 403 限流响应后冷却 15 分钟，期间直接读取备用缓存，之后恢复优先读取 Gitee。页面注明缓存来源与更新时间，心跳仍按原始采集时间判断；两边都不可用时保留已读取的数据并标记状态待确认。浏览器不写 KV，也不触发 Cloudflare 即时抓取 Gitee。启用此功能需先部署 Cloudflare Worker，再发布 Dashboard；仅更新页面不会自动部署缓存接口。
服务、Gitee 访问、环境/磁盘、任务交付与 Codex 异常分别展示；Codex 连接、认证、限流等分类需要服务器更新后的结构化健康字段，旧快照显示未上报，不推测错误原因。心跳过期时不继续展示旧的绿色状态，浏览器读取失败也不判成服务器断网。按需 oneshot 服务未运行不算故障；不再读取旧 watchdog 文件。
「Cloudflare 告警记录」匿名读取同一健康仓库最近 50 条更新的 Issues，筛选当前 Worker 的自动告警标记，展示最近 10 条及详情入口。它与实时健康快照分开展示：Issue 已关闭不代表服务已经恢复，没有 Issue 也不表示外部监测已启用。页面不触发通知；无人打开页面时的检测和 Issue 写入由独立 [Cloudflare Worker](../scripts/local_ci/maintenance/cloudflare/README.md) 执行，无需为每次告警重新发布 Pages。
本地静态预览不代表已部署 Pages，也不代表真实工具链或生产门禁验收通过。

任务执行与恢复区展示动作、次数、截止时间和真实容器状态；结果上传等待独立显示。近 7 天异常与恢复记录合并服务器事件及 Cloudflare 只读缓存，默认显示 20 条，可展开至最多 100 条。页面读取缓存不写 KV。任务/上传采集失败显示未知，不用空列表宣告恢复。健康页面测试：`node --test scripts/local_ci/tests/dashboard.test.cjs`。
