# 服务器 Codex 部署交接：LLVM 隔离与运维恢复

本文合并 LLVM 隔离与运维恢复更新，作为此次部署的统一入口。四个新增 profile 已完成，不再重复安装或补建；LLVM 专项验收细节可参考 `VARIANT_LLVM_DEPLOYMENT.md`。

## 1. 已完成事项与部署目标

- LLVM 隔离代码：`c58e53fed29aa048a6859ea65136619e39197ad9`。
- 已整合 LLVM 更新并推送的运维恢复代码：`78f7675668d85438a587c6d1df5d31388acb886a`，GitHub 分支 `local-ci-unified`。
- 同事完成的 Triton **3.2、3.4、3.5、3.8** LLVM/profile 配置已在 `ba7e5ea` 提交入库；本轮修复以该提交为基础。保留现有成果，不再安装 LLVM、重新创建这四个 profile。
- 本轮还包含 Cloudflare 活动 Issue 查询分页上限、恢复原因保留与页面记录折叠。部署请选择包含这些修改的本轮最终完整 SHA。
- 本文不是部署完成记录。服务器 Codex 应先核对实际状态，已经部署且有验证记录的部分不重复执行。

入口仍为 GitHub PR/push → Gitee → 服务器 → Gitee 结果 → GitHub 回写。沿用单任务执行、检查名称、head 回写、现有健康仓库和 Cloudflare 资源。

## 2. 部署前先确认完整配置，避免覆盖同事成果

**`ba7e5ea` 已补齐八个 profile，原先配置未入库的问题已解决。** 部署时仍需核对目标提交与服务器现有配置一致，不要使用只有四个 profile 的旧 `78f7675` 覆盖服务器。

原因：`control_update.py` 读取目标 SHA 的已提交配置；`install.py` 读取它所在 checkout 的 HEAD 已提交配置。两者均完整同步 `scripts/local_ci/prepare/config.example.json` 到运行 JSON，不合并额外 profile。安装器的 `--config` 是运行配置写入位置，不是配置来源。

比较服务器运行 JSON、当前控制提交配置、最终目标提交配置，只报告非敏感差异。最终提交必须同时具有：

- `78f7675` 的代码与新增恢复参数。
- 同事完整的八个版本配置：3.0、3.1、3.2、3.3、3.4、3.5、3.6、3.8；3.5.1 源码对应 profile 版本 3.5。
- 已核验的 LLVM SHA、挂载路径、目录摘要、环境变量、共享镜像与后端能力。仅已验证的 3.0 开启后端。
- 新的 `monitor_services`，不包含旧 watchdog；不再使用 `branch_profiles`。

选择包含 `ba7e5ea` 和本轮修复的最终完整 SHA，确认已同步到配置指定的 Gitee 控制分支。如服务器还有未入库的配置差异，先私有备份并查明来源，再决定同步，不能直接覆盖。不要在 live 控制目录改文件，也不要整份使用旧运行 JSON 覆盖新增恢复配置。

## 3. 先更新 Cloudflare 和页面

已经更新的观察端只核对版本和执行记录。若服务器没有这些账户的既有授权，由维护者在原部署端执行并交接结果；不另建账户、Worker、KV 或凭据。

1. 从包含本轮更新的 checkout 更新现有 Cloudflare `local-ci-alert`：

   ```bash
   wrangler deploy --config scripts/local_ci/maintenance/cloudflare/wrangler.jsonc
   ```

   保留 `ALERT_STATE` KV、现有 `GITEE_TOKEN`、每 5 分钟 Cron 和 Issue 历史。确认 scheduled 调用成功，只有一个监测程序维护告警。
2. 使用 main 的 `CI Gateway` 手动发布：`mode=publish`，任务 ID 留空。它会解析并 checkout 控制分支代码；记录实际发布 SHA。无需修改 main YAML 或新增 Workflow。
3. 查看 [Worker 页面](https://likehupochuan.github.io/triton-anchor/worker.html)及 [Cloudflare 缓存](https://local-ci-alert.2272640910.workers.dev/health)。页面仍 Gitee 主读、Cloudflare 备用；旧快照缺字段显示“未上报”，读取失败不等于服务器故障。

## 4. 服务器准备

使用普通 CI 用户 `jiwang_ci`、user systemd 和现有 Rootless Docker，不以 root 执行部署。以下路径先与服务器运行配置核对：

| 用途 | 路径 |
| --- | --- |
| 控制 checkout | `/home/jiwang_ci/local_ci/control_anchor` |
| 运行配置 | `/home/jiwang_ci/local_ci/config/local-ci.json` |
| 私有凭据 | `/home/jiwang_ci/local_ci/config/credentials.env` |
| 持久状态 | `/home/jiwang_ci/local_ci/state` |

记录当前控制 SHA、配置非敏感差异、服务状态及在途任务。私有备份运行配置、units、Journal 元数据，完整保留原 runs、封存结果、outbox 与预算，不删除或移动状态目录。

等待执行中任务自然结束。有仍有效、需要当前控制版本恢复的未封存任务时，先处理该任务；只有封存结果待上传不阻挡升级。确认旧进程停止，task/task-cleanup 容器已由现有清理机制处理。不要为了升级取消任务、清空证据或强制释放锁。

凭据沿用已有私有注入方式，文件归 CI 用户所有且权限 600。安装器自行读取 `--credentials-env`；控制更新没有这个参数，需在既有私有环境中运行。不要输出 Token、完整 session 或凭据文件内容。

## 5. 精确版本更新与首次服务迁移

将占位符替换为第 2 节确定的、包含完整配置的最终 40 位 SHA。从 live checkout 预览：

```bash
cd /home/jiwang_ci/local_ci/control_anchor
CI_DEPLOY_REVISION='<最终已审核且包含完整配置的40位SHA>'
CI_PYTHON=/home/jiwang_ci/local_ci/local-ci-control-venv/bin/python
"$CI_PYTHON" scripts/local_ci/prepare/control_update.py \
  --config /home/jiwang_ci/local_ci/config/local-ci.json \
  --expected-revision "$CI_DEPLOY_REVISION"
```

核对 checkout 干净、目标来自配置的 Gitee 控制分支且可快进、八个 profile 不丢失，`control_root`、`state_dir`、Python、Rootless Docker 等部署锚点没有意外变化。不要直接把仍只有四个 profile 的 `78f7675` 作为最终目标。

确认空闲后停止 Worker，再应用：

```bash
systemctl --user stop triton-anchor-local-ci.service
"$CI_PYTHON" scripts/local_ci/prepare/control_update.py \
  --config /home/jiwang_ci/local_ci/config/local-ci.json \
  --expected-revision "$CI_DEPLOY_REVISION" --apply
```

返回 `state: updated` 或 `current` 后核对实际 HEAD 和配置。`deferred-active-task` 即便退出码为 0 也表示未完成部署，应查明占用、等待后再试。失败时检查实际代码/配置是否已经切换，不盲目继续或启动旧版本。

**首次迁移还要运行新版安装器。** 旧更新器已经加载的 Python 不会随 checkout 自动更新，首次切换不能保证执行了新版 watchdog 清理。控制更新可能已启动 Worker；再次确认空闲，如果已接收新任务，等它完成后再停：

```bash
systemctl --user stop triton-anchor-local-ci.service
"$CI_PYTHON" scripts/local_ci/prepare/install.py \
  --config /home/jiwang_ci/local_ci/config/local-ci.json \
  --credentials-env /home/jiwang_ci/local_ci/config/credentials.env
```

审阅预览后应用：

```bash
"$CI_PYTHON" scripts/local_ci/prepare/install.py \
  --config /home/jiwang_ci/local_ci/config/local-ci.json \
  --credentials-env /home/jiwang_ci/local_ci/config/credentials.env --apply
```

安装器会自行获取控制锁、验证完整配置、逐 profile 准备/校验 runtime 和基础 canary，备份/安装 units，清理旧 watchdog，刷新 systemd，同步配置并启动 Worker 与 timers。不要在外层持有同一把 `control.lock`，也不需要额外重复全量 `ensure_image`。

这一步复用现有 LLVM 与共享镜像。runtime 验证不等于全部版本真实 build/install/smoke 通过。首次“控制更新 + 新版安装器”可能重启 Worker 两次；后续新版控制更新已含幂等清理，不必每次重装。

## 6. 检查部署结果

确认实际 HEAD、最终提交配置与运行 JSON 一致，八个 profile 全部保留。使用以下命令核对服务：

```bash
git rev-parse HEAD
systemctl --user is-active triton-anchor-local-ci.service
systemctl --user is-active triton-anchor-local-ci-health.timer
systemctl --user is-active triton-anchor-local-ci-retention.timer
systemctl --user is-enabled triton-anchor-local-ci-watchdog.service triton-anchor-local-ci-watchdog.timer
systemctl --user is-active triton-anchor-local-ci-watchdog.service triton-anchor-local-ci-watchdog.timer
```

旧 watchdog 应为 disabled/not-found、inactive；这两项查询的非零退出码可能是预期结果。保留 Gitee 历史 watchdog 文件和 Issues，不删除历史。health/control-update 等 oneshot 正常结束后 inactive 属于正常状态。安装器不更新 `control-update.json` 的成功时间，不能仅凭其中旧记录判定本次安装失败。

用现有 unit 触发一次健康采集，复用原凭据环境：

```bash
systemctl --user start triton-anchor-local-ci-health.service
```

等待新上报与至少一轮 Cloudflare 检测，核对采集时间和身份，避免误用旧缓存：

- 有任务时显示阶段、恢复动作、次数、下次尝试、截止时间；空闲或缺字段不制造故障。
- 容器运行状态、退出码/OOM、systemd 子状态来自实际查询；查询失败显示未知。
- 页面有独立上传等待区，以及近 7 天“异常与恢复记录”；同一运行的连续恢复过程折叠为摘要，默认展示 20 组，明细与更多记录可展开。已关闭告警默认折叠，不依赖旧 watchdog 数据。
- 公开事件每任务最多 20 条、全局最多 100 条，不含原始异常、凭据、完整 session 或私有路径。
- Cloudflare 只有获得更晚且新鲜、对应字段明确正常的快照才确认恢复。读取失败、过期、缺字段、旧快照不能误关 Issue；页面读取不写 KV。

## 7. 部署时应保留的恢复行为

| 场景/预算 | 应有行为 |
| --- | --- |
| 已封存 | 按原 task_id + run_id 补传同一结果；Docker 故障不导致重测 |
| 完整有效报告，包括 fail | 确认旧执行停止，按宿主 checkpoint 继续封存，不重测变绿 |
| 无法接续 | 旧执行停止后，在原预算内重建隔离环境 |
| Codex | 最多启动 10 次；首次起共享 6 小时；普通重试 30 秒 |
| 新 session | 最多一次且计入 10 次；无效 session 或两次 resume 无有效进展后才切换 |
| 认证/限流 | 认证等待凭据变化或显式恢复；限流退避，不靠换 session 绕过 |
| 环境/准备 | 创建最多 3 次；等待依赖不耗次数；准备恢复 6 小时，与已有执行截止时间取更早者 |
| 封存 | 瞬态 I/O 共 3 次，间隔 30、60 秒；无效报告不反复封存 |
| 上传 | 前 5 轮间隔 60、120、300、300 秒；之后每小时补传并告警 |
| 无进展 | 30 分钟提示、60 分钟复查；存活但静默不自动终止 |

重启、重建、新 session 与同任务手动恢复不刷新预算。旧预算无法确定时停止自动重跑，保留证据并提示重新派发。服务停机不是 PR 取消，真实测试失败正常发布 fail。

base/candidate 共用一个任务容器，但工作区、venv、LLVM 和环境变量独立，按各自冻结源码选 profile。新结果保留 `environment.variants.base/candidate`；续封存使用原宿主 checkpoint，旧版已封存结果不改写。

## 8. 正常运行与恢复验收

复用同事已有的八个版本验收记录，不为本次部署重新安装或重跑全部版本。没有真实构建证据的版本标明“配置/runtime 已验证，真实构建待验证”。

**正常链路：** 使用专用测试 PR/push，记录 task_id、run_id、head/tested SHA。前置检查、审批（如需）、派发正常；Summary 仍在成功派发后出现，pending 进度更新不增加检查项，终态不退回 pending。GitHub、Gitee 原结果与 Dashboard 一致。

至少核验一次跨 LLVM 任务，可复用已有可验证记录。确认两侧实际构建及 `environment.variants` 没有混用 LLVM。3.0/3.1 虽共享 LLVM，profile/后端能力仍区分；性能环境不同为 `not_comparable`。未执行 base 时，不宣称已验证两侧构建。

**恢复演练：** 使用独立演练状态与结果目标，不启动第二个生产 Worker。仅让演练结果上传失败，生产 health 保持可用；记录封存摘要、run_id、Codex 次数和截止时间。重启演练 Worker 后只补传原结果，不新建任务容器、不重新启动 Codex。恢复访问后确认同一结果上传，摘要、run_id 与预算保持。

Issue 开关验收仅在已有隔离健康快照、监测配置和告警目标的演练环境进行；生产监测不会自动观察独立演练状态目录。没有这套条件时，使用现有 Cloudflare 自动测试验证告警逻辑，并将真实 Issue 恢复验收列为待执行。不为本次演练新增 Cloudflare 资源，不篡改生产快照伪造验收。

不通过停整机 Docker、破坏 LLVM 目录或终止静默存活任务演练。其他崩溃边界可在开发/演练环境复验现有自动测试：

```bash
python3 -m pytest scripts/local_ci/tests -q
node --test scripts/local_ci/maintenance/cloudflare/worker.test.mjs scripts/local_ci/tests/dashboard.test.cjs
```

`78f7675` 桌面整合时已通过 268 项 Python、48 项 JavaScript 测试及 Ruff 硬错误检查；不替代服务器 Docker/systemd 与真实任务验收。

## 9. 失败处理与反馈

配置、依赖、锁或安装失败时保留已完成步骤与证据，不删 Journal、重置预算、清空 outbox 或强制 checkout。回退前停 Worker 并保存新版状态，先确认旧代码能理解未完成任务。安装器 `--rollback` 仅恢复 units，不是代码、配置和任务状态的整体回滚。

最终反馈：实际完整控制 SHA；完整 profile 配置的来源与保留结论；Cloudflare 部署版本、Pages 运行链接；服务和 watchdog 清理结果、备份位置；新健康快照时间；正常/跨 LLVM 任务 ID；恢复演练的原摘要/run_id 和预算保持情况；尚未完成的外部部署或真实验收。只输出脱敏信息。
