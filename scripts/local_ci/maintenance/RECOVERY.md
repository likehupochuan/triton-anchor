# Local CI 运维监控与异常恢复

## 运行规则

入口仍为 GitHub PR/push → Gitee 冻结任务 → 服务器执行 → Gitee 结果 → GitHub 接收器。最多一个任务执行线程，主循环继续维护心跳、取消和恢复核对，单独一个上传线程处理其他运行的补传；没有新增恢复服务或定时 GitHub 工作流。

任务阶段为 preparing / running / sealing / publish_pending / published，恢复状态独立记录。阶段、动作、原因类别、次数、截止时间和最近恢复结论写入现有 Journal。宿主机保存封存所需的上下文，候选代码不能替换该上下文。

1. 已封存：校验原结果并补传，Docker 不可用也不重测。
2. 有完整有效执行报告：确认原执行已停止，继续封存，pass 与 fail 同样处理。
3. 没有可靠结果：确认旧执行停止后，在原预算内重建隔离环境。
4. 真实测试失败：封存并发布 fail，不作为临时网络故障自动重测。
5. 服务停机：保存可恢复状态；PR 关闭、取消、任务替代另行记录，不混为取消。

base 与 candidate 仍共用一个任务容器，但各自采用冻结源码对应的 LLVM、工作区、venv 和环境变量。宿主 checkpoint 与最终结果保留双方 `environment.variants`；续封存直接使用原 checkpoint，不重新选择环境。重建时重新校验双方冻结源码，不改写原 task；旧版已封存的平面 environment 保持原字节。依赖准备见 [双版本 LLVM 部署说明](../prepare/VARIANT_LLVM_DEPLOYMENT.md)。

可恢复且仍有效的未封存任务保留当前控制版本，暂缓控制更新。只剩封存上传时不阻止升级。沿用现有任务唯一执行与控制代码锁。

## 预算与恢复动作

| 项目 | 默认上限或等待 |
| --- | --- |
| Codex 启动 | 10 次，首次计入 |
| Codex 执行 | 首次启动起共享 6 小时 |
| 普通重试 | 间隔 30 秒 |
| 新 session | 最多一次，计入 10 次；session 无效或连续两次 resume 无有效进展后才切换 |
| 环境创建 | 3 次，首次计入；等待 Docker/磁盘等依赖不消耗次数 |
| 准备恢复 | 首次基础设施故障起 6 小时，与已有执行截止时间取更早者 |
| 封存瞬态 I/O | 共 3 次，间隔 30、60 秒；无效报告直接结束，不反复封存 |
| 上传 | 前 5 轮间隔 60、120、300、300 秒；此后每小时补传，保留原结果并告警 |
| 无有效进展 | 30 分钟提示，60 分钟复查；进程存活但静默时不自动终止 |

所有次数与截止时间持久化。重启、重建、更换 session 和同任务手动恢复均不刷新。额度耗尽需要重新派发新任务，不能删除 Journal 来继续。

认证错误等待凭据变化或明确恢复；限流有界退避，不通过更换 session 规避。恢复分类仅依据顶层 CLI 错误、进程退出及真实容器状态，不扫描测试输出中的关键词。静默告警不等于进程挂死；只有明确退出、容器故障或达到总截止时间才恢复/结束。

如需显式恢复同一任务，先停止 Worker（避免绕过单实例锁），运行 `python3 scripts/local_ci/agent_ci/worker.py --config /home/jiwang_ci/local_ci/config/local-ci.json --resume <TASK_ID>`，再启动 Worker。此操作不清除次数或截止时间；耗尽任务拒绝自动重跑。已封存的待上传任务仅提前安排补传。认证凭据替换后会重新检测，不要把凭据放进命令行或公开快照。

旧未完成状态从已有日志和事件保守迁移预算；无法确认剩余额度则停止自动重跑，说明需重新派发。历史封存结果保持原样。

## 观察入口

- Worker 页面：当前执行/恢复、次数、原因、下一次尝试、截止时间；独立上传等待区；近 7 天异常与恢复记录（默认 20 条可展开）。
- Gitee 健康仓库 Issues：连续事件复用一个 Issue，故障、动作或结论变化才更新。
- PR 或提交的现有 Local CI Summary：派发后为 pending，最多每五分钟读取配置健康源，显示阶段与恢复尝试；最终结果优先，不增加检查项。
- 本机 Journal 和 systemd 日志：保留具体错误和私有诊断，公开快照仅包含允许字段。

Cloudflare 无法读取 Gitee 时主机状态为未知；快照过期只说明上报停止。恢复需要晚于故障证据的新鲜、对应字段明确正常的快照。旧数据、缺字段、另一个任务或待关闭 Issue 的旧重试都不构成恢复证据。KV 每轮最多两次写入，页面 GET 不写入。

## 兼容部署顺序

本次代码交付不自动推送、部署或重跑生产任务。确认版本后按以下顺序操作：

1. 先更新现有 `local-ci-alert` Cloudflare Worker，保留 KV、凭据、五分钟 Cron 和已有 Issue。按 [Cloudflare 部署说明](cloudflare/README.md) 执行已有部署命令。旧健康字段仍可读；没有新恢复字段时显示未上报，不制造新故障。
2. 发布 Dashboard：使用 main 的 CI Gateway 手动 `mode=publish`，不带任务 ID。现有 main 接收和发布工作流会先解析并 checkout 最新 local-ci-unified；此次无需修改其 YAML 或新增 Workflow。确认 worker 页面加载新版脚本。
3. 将已审核控制提交同步到现有 Gitee 控制镜像；在服务器维护窗口停止 Worker，确认任务进程已退出，保留 state/runs/outbox。使用现有精确 SHA 控制更新流程切换。
4. 首次迁移到此版本后，运行**新版**安装器。旧更新器在 checkout 前加载的 Python 不会自动执行新清理逻辑，因此不能只依赖第一次旧版升级。先预览，确认再 `--apply`：

```bash
python3 scripts/local_ci/prepare/install.py \
  --config /home/jiwang_ci/local_ci/config/local-ci.json \
  --credentials-env /home/jiwang_ci/local_ci/config/credentials.env
# 审阅输出后对同一命令添加 --apply。
```

安装与控制更新共用配置同步和旧 unit 清理，不需要手动创建覆盖 JSON，也不删 Gitee 历史 watchdog 文件。检查旧 `triton-anchor-local-ci-watchdog.service` / `.timer` 已停用，health timer 和 Worker 正常；后续升级清理幂等。若预览发现部署锚点改变，按安装器要求处理，不能热切换 state_dir 等持久状态目录。

5. 等待一次 health 采集和 Cloudflare 周期，核对新鲜 collected_at、真实服务/容器状态、页面及 Issues。不要用旧缓存判断部署失败或恢复成功。

回退时先停 Worker 并保存新版 Journal、封存结果和 outbox；不要让不理解新增预算的旧 Worker 自动处理未完成任务。页面和 Cloudflare 可独立回退，生产恢复前确认状态兼容。

## 本地验证

在仓库根目录运行相关 Python 测试、Cloudflare/页面 JavaScript 测试和静态检查。测试使用临时目录与模拟故障，不要求生产 Token，不创建真实 Issues。

```bash
python3 -m pytest scripts/local_ci/tests -q
node --test scripts/local_ci/maintenance/cloudflare/worker.test.mjs scripts/local_ci/tests/dashboard.test.cjs
```

发布前还应执行页面测试和 `node --check`。缺真实 Docker/systemd 的开发机只能验证模拟故障边界，生产演练应按下列步骤执行。

## 上线验收演练

### 一次正常运行

派发一个范围明确的测试 PR 或 push，记录 task_id、head/tested SHA。检查前置检查按顺序出现，审批（如需）和派发成功后才出现 Summary。观察 Journal 的 preparing → running → sealing → publish_pending → published，health 中 task/run 身份一致；PR pending 描述变化但检查数量不增加。最终 GitHub、Gitee 原结果与 Dashboard 结论一致，没有额外重测或告警。

### 一次故障与恢复

使用专用演练任务和隔离配置，避免中断生产任务。首选“封存后暂停上传”演练：通过测试适配器或仅影响演练结果仓库的网络故障，让上传失败，保持 health 仓库可用。记录封存 result.json 的摘要与 run_id，重启演练 Worker。确认无需 Docker/新容器即可补传，Codex 启动次数和执行截止时间保持，页面显示上传等待及次数；达到快速轮次后进入小时补传。恢复演练结果仓库访问后，确认同一份摘要/同一 run 发布成功，新的健康快照证明交付恢复，Cloudflare 更新并关闭相应事件。

另用回归用例覆盖封存后未入队崩溃、服务响应丢失、有效 fail 报告封存前重启、认证等待和 session 切换。不要为了演练停止整机 Docker，也不要在聊天或公开日志中输出 Token、session 或私有路径。对 live 但静默任务只确认 30/60 分钟提示，不主动杀死它。
