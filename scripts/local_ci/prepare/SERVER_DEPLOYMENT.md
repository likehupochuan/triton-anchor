# 服务器 Codex 部署交接：LLVM 隔离与运维恢复

本文只交接服务器上的 LLVM 隔离与运维恢复部署、检查和验收。Cloudflare 更新、Dashboard 发布及外部展示验收由桌面维护者完成，不属于服务器 Codex 的执行范围。

四个新增 profile 已完成，不再重复安装或补建。`VARIANT_LLVM_DEPLOYMENT.md` 仅作为 LLVM 专项验收参考，其中补齐四个 profile 的旧步骤不再执行。

## 1. 已完成事项与部署目标

- LLVM 隔离代码：`c58e53fed29aa048a6859ea65136619e39197ad9`。
- 已整合 LLVM 更新并推送的运维恢复代码：`78f7675668d85438a587c6d1df5d31388acb886a`，GitHub 分支 `local-ci-unified`。
- 同事完成的 Triton **3.2、3.4、3.5、3.8** LLVM/profile 配置已在 `ba7e5ea` 提交入库；本轮修复以该提交为基础。保留现有成果，不再安装 LLVM、重新创建这四个 profile。
- 本轮服务器修复保留连续恢复过程中的故障原因。已审核代码基线为 `70fa331bcda205b73436129434c3e13f70e79937`，包含上述 LLVM、完整配置与运维恢复更新。使用该精确版本；如维护者交接了更新的完整 SHA，应先确认它包含此基线。
- 本文不是部署完成记录。服务器 Codex 应先核对实际状态，已经部署且有验证记录的部分不重复执行。

任务仍由 GitHub PR/push 经 Gitee 到达服务器，服务器执行后上传 Gitee 结果。沿用单任务执行和现有健康仓库。

## 2. 部署前先确认完整配置，避免覆盖同事成果

**`ba7e5ea` 已补齐八个 profile，原先配置未入库的问题已解决。** 部署时仍需核对目标提交与服务器现有配置一致，不要使用只有四个 profile 的旧 `78f7675` 覆盖服务器。

原因：`control_update.py` 读取目标 SHA 的已提交配置；`install.py` 读取它所在 checkout 的 HEAD 已提交配置。两者均完整同步 `scripts/local_ci/prepare/config.example.json` 到运行 JSON，不合并额外 profile。安装器的 `--config` 是运行配置写入位置，不是配置来源。

比较服务器运行 JSON、当前控制提交配置、最终目标提交配置，只报告非敏感差异。最终提交必须同时具有：

- `78f7675` 的代码与新增恢复参数。
- 同事完整的八个版本配置：3.0、3.1、3.2、3.3、3.4、3.5、3.6、3.8；3.5.1 源码对应 profile 版本 3.5。
- 已核验的 LLVM SHA、挂载路径、目录摘要、环境变量、共享镜像与后端能力。仅已验证的 3.0 开启后端。
- 新的 `monitor_services`，不包含旧 watchdog；不再使用 `branch_profiles`。

确认第 1 节的目标完整 SHA 已同步到配置指定的 Gitee 控制分支。如服务器还有未入库的配置差异，先私有备份并查明来源，再决定同步，不能直接覆盖。不要在 live 控制目录改文件，也不要整份使用旧运行 JSON 覆盖新增恢复配置。

## 3. 服务器准备

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

## 4. 精确版本更新与首次服务迁移

以下使用第 1 节的已审核目标；如维护者已指定更新的完整 SHA，核对其包含此基线后替换。目标尚未出现在 Gitee 控制分支时先反馈，不改换来源或强制更新。从 live checkout 预览：

```bash
cd /home/jiwang_ci/local_ci/control_anchor
CI_DEPLOY_REVISION='70fa331bcda205b73436129434c3e13f70e79937'
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

**首次迁移还要运行新版安装器；已完成并验证过此次服务迁移则跳过本段。** 旧更新器已经加载的 Python 不会随 checkout 自动更新，首次切换不能保证执行了新版 watchdog 清理。控制更新可能已启动 Worker；再次确认空闲，如果已接收新任务，等它完成后再停：

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

## 5. 检查部署结果

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

检查本次 health unit 的执行结果、生成的本地快照及上传记录；必要时读取 Gitee 中的 `worker-health.json` 确认上传内容。记录采集时间、Worker 身份和上报结果，避免用旧文件当作本次验证：

- 有任务时上报阶段、恢复动作、次数、下次尝试、截止时间；心跳、采集时间与任务有效进展分别记录。
- 容器运行状态、退出码/OOM、systemd 子状态来自实际查询；查询失败标记未知，正常结束的 oneshot 不误报为常驻服务停止。
- 上传等待按原 `task_id + run_id` 记录，保留近期终态和恢复事件；连续恢复的下一次尝试仍保留故障原因。
- 公开事件每任务最多 20 条、全局最多 100 条，保留近 7 天；不含原始异常、凭据、完整 session 或私有路径。
- 空闲或当前没有相应事件时，不伪造任务和恢复数据。上传失败应保留本地快照与错误类别并反馈。

## 6. 部署时应保留的恢复行为

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

## 7. 正常运行与恢复验收

复用同事已有的八个版本验收记录，不为本次部署重新安装或重跑全部版本。没有真实构建证据的版本标明“配置/runtime 已验证，真实构建待验证”。

**正常任务：** 使用维护者派发的专用测试任务，或复用已有可验证的运行记录，不为部署自行新建 PR。记录 task_id、run_id、head/tested SHA；核对服务器按准备、执行、封存、上传推进，封存摘要和 Gitee 上传结果一致，任务运行期间 Worker 心跳仍持续更新。没有新任务时如实标明真实任务验收待执行。

至少核验一次跨 LLVM 任务，可复用已有可验证记录。确认两侧实际构建及 `environment.variants` 没有混用 LLVM。3.0/3.1 虽共享 LLVM，profile/后端能力仍区分；性能环境不同为 `not_comparable`。未执行 base 时，不宣称已验证两侧构建。

**恢复演练：** 使用独立演练状态与结果目标，不启动第二个生产 Worker。仅让演练结果上传失败，生产 health 保持可用；记录封存摘要、run_id、Codex 次数和截止时间。重启演练 Worker 后只补传原结果，不新建任务容器、不重新启动 Codex。恢复访问后确认同一结果上传，摘要、run_id 与预算保持。

没有独立演练状态与结果目标时，不修改生产环境凑验收，将恢复演练列为待执行并说明所缺条件。不通过停整机 Docker、破坏 LLVM 目录或终止静默存活任务演练。代码级测试已在桌面侧执行，服务器重点验证实际 Docker/systemd、健康上报、任务和原结果恢复，不要求重新运行 Cloudflare 或页面测试。

## 8. 失败处理与反馈

配置、依赖、锁或安装失败时保留已完成步骤与证据，不删 Journal、重置预算、清空 outbox 或强制 checkout。回退前停 Worker 并保存新版状态，先确认旧代码能理解未完成任务。安装器 `--rollback` 仅恢复 units，不是代码、配置和任务状态的整体回滚。

最终反馈：实际完整控制 SHA；八个 profile 配置的来源与保留结论；服务和 watchdog 清理结果、私有备份位置；新健康快照的采集时间及上传结果；正常/跨 LLVM 任务 ID 和验证范围；恢复演练的原摘要/run_id 及预算保持情况；尚未完成的服务器步骤或真实验收。只输出脱敏信息。
