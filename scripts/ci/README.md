# Local CI Gateway 与接收器

Gateway 在 GitHub 冻结任务、执行前置检查并投递到 Gitee；Worker 从 Gitee 取得源码，
执行后发布结果，接收器回写 GitHub 状态、PR 评论和 Dashboard。

## 工作流分工

| 入口 | 所在分支 | 职责 |
| --- | --- | --- |
| `ci-request.yml` / CI Request | `main` 接收 PR 与手动请求；各源码分支接收 push | 将任务路由到控制分支 |
| `ci-gateway.yml` / CI Gateway | `local-ci-unified` | 冻结任务、前置检查、审批与投递 |
| `ci-gateway.yml` / CI Gateway | `main` | 接收结果、回写状态与评论、发布 Dashboard |

需要 push 自动触发的分支须包含 `ci-request.yml`；PR 入口在默认分支运行。
路由不限制目标分支，实际环境由冻结源码中的 Triton 版本和 LLVM SHA 决定。

PR 任务冻结目标 base、贡献者 head 和精确 merge/tested SHA。
分支任务使用指定分支 HEAD 作为 tested，其第一父提交作为 base。
源码与随任务检出的子模块固定 refs 全部推送到 Gitee 后，才发布不可变 `tasks/`、
当前任务 `current` 与取消标记 `cancel`。源码 refs 包含完整 task ID。

FlagGems 使用服务器 profile 的固定只读目录；其子模块指针不切换服务器依赖。
其他子模块固定到被测 Git 对象。

## 前置检查与审批

检查按成功依赖推进，状态回写可与下一检查并行：

1. CI Request 校验请求后写入 pending Summary，再派发 Gateway。
2. Gateway 冻结任务并取得执行归属，创建 Basic 并接管 Summary；PR 任务校验信息后执行 Basic CI。
3. Basic 通过后执行 API Compatibility，API 通过后执行 Security Gate。
4. 同仓库任务在前置检查通过后投递；外部 fork 在检查及回写完成后生成审批卡。
5. 外部 fork 经 `local-ci-fork-approval` environment 审批，复查冻结身份后投递。
6. 源码与任务成功发布到 Gitee 后更新 Summary 的等待说明，并启动接收器。

前置失败仅结束已到达的阶段。外部审批拒绝或校验失败终止审批阶段，
取消记录为错误状态，不创建 Dispatch。
PR 关闭、转草稿、目标变化或新增提交时结束失效任务的待定状态，并通知服务器停止执行。

审批卡同时写入 PR 与工作流摘要，展示前置结论、贡献者与来源、冻结 SHA、
改动数量和按路径归类的范围、diff 链接及审批入口。
PR 正文提供概述、影响范围和验证情况，创建 PR 时自动使用默认分支上的
[PR 模板](https://github.com/likehupochuan/triton-anchor/blob/main/.github/PULL_REQUEST_TEMPLATE.md)；push/manual 不要求这些字段。

只有 environment 明确记录人工拒绝，才向仍匹配的 PR 通知审批未通过；
审批校验错误或取消按对应状态处理。任务取消仅更新状态和 Gitee 停止标记。

## 检查状态

Basic、API、Security、Approve、Dispatch 和 Summary 均使用 Commit Status。
PR 状态写入 `head_sha`，实际测试及证据对应冻结的 `tested_sha`；
分支任务状态写入 `tested_sha`。控制分支自身 push 也发布前置阶段状态。

`Summary` 表示整轮验证的结论，阶段检查只在执行到该阶段时创建。
Summary 从 CI Request 确认有效请求后为 `pending`，覆盖 Gateway 排队与初始化、前置检查、外部审批、
任务投递、服务器执行与结果接收；阶段切换不会使整轮验证短暂显示为全部成功。

| 情况 | Summary |
| --- | --- |
| 验证进行中、等待审批或结果 | `pending` |
| 最终结果及必需证据校验通过，验证成功 | `success` |
| PR 信息不完整、前置检查或服务器验证失败、人工拒绝审批 | `failure` |
| 取消、任务失效、审批配置异常、投递失败或接收超时 | `error` |

前置失败不创建后续未执行项，Summary 指出失败原因并链接对应运行。
请求阶段按 CI Request run ID 与 attempt 标记归属；Gateway 使用传入的 `request_id`
接管后绑定冻结 task ID。新请求会阻止旧任务回写，旧请求及其失败收尾不能覆盖新请求或已接管的任务。
派发失败或 Gateway 初始化中断时，尚未接管的 Summary 收尾为 `error`。
关闭或转为草稿的 PR 只执行取消流程，不创建新的等待状态。
直接派发 Gateway 时不填写 `request_id`，Summary 在可信任务初始化时创建。
新任务及完整重跑重新初始化 Summary，旧运行不能覆盖新运行的结论；
重复初始化不会重新打开已结束的阶段。结果接收器只在成功投递后接管 Summary。
`Local CI Approve` 只用于外部 fork，同仓库任务不创建该项。
这些状态报告验证进度与结果，不配置强制合并规则，也不保证 Merge 按钮置灰。

阶段与工作流绑定 task ID、workflow run ID 和 attempt。
Basic 状态标识当前执行归属，接收器还要求对应 Dispatch 已成功；
迟到的运行不能覆盖当前状态。相同回写去重，状态变化保留历史，以最新记录为准。
最终测试结论来自 `Summary`，各 Actions 作业记录对应阶段的调度或交付结果。

检查名称、状态与失败说明使用英文，审批卡、Agent 解释和 PR 结果使用中文。
PR 结果按 `result + task_id + run_id + result_digest` 去重，每次运行追加一条结果评论。
评论区分 PR 提交与合并后验证提交，列出实际执行的检查、阻塞项及报告链接；
完整的选测原因和不适用项在 Dashboard 展示。发现保留风险等级，缺失时显示“未标注”。
“需要关注的发现”按独立缺陷列出结论、分析和代码证据；失败检查与审查是验证记录，
不重复追加为新的合入阻塞。没有阻塞 findings 时，失败报告使用阻塞原因作为兜底。
环境、工具和证据发布限制单列“限制说明”，说明对结论的影响；必要验证未完成仍保持非通过状态。

## 手动派发与接收

### 分支验证

在 Actions → CI Request → Run workflow 选择 `main`，填写：

| 字段 | 内容 |
| --- | --- |
| `source_branch` | 实际被测分支 |
| `requested_sha` | 可选的完整 HEAD SHA，用于拒绝分支漂移 |

发起者需拥有 write、maintain 或 admin 权限。
路由统一派发到 `local-ci-unified`，控制提交由路由确定。

### 完整验证

在 Actions → CI Gateway → Run workflow 选择 `local-ci-unified`：

| 字段 | 内容 |
| --- | --- |
| `mode` | `run` |
| `worker_revision_sha` | 控制分支当前 HEAD 的完整 SHA，必须与工作流提交一致 |
| `full` | `true` |
| `pr_number` | PR 编号；分支任务填 `0` |
| `source_branch` | 分支任务填写被测分支；PR 从自身信息确定 |
| `requested_sha` | PR head 或分支 HEAD 的完整 SHA |
| `action` | 手动分支任务填 `manual`；PR 任务留空 |

full 要求全部可用工具对应的验证，外部 fork 使用同一审批流程。
base/candidate 的 profile 与能力见 [Local CI 环境](../local_ci/README.md#环境与生命周期)。

### 结果接收

任务投递成功后，在 `main` 启动 `mode=receive`，按 task ID 每分钟查询一次；
每轮最多 5 小时 40 分钟，共最多 3 轮。收到有效结果或任务失效时结束，
轮次耗尽报错，服务器任务按自身状态继续处理。

补收时在 CI Gateway 选择 `main`、`mode=receive`，
填写原 `task_id`，`receiver_round` 为 `1`。补收使用已有结果，不重新投递构建。

每轮解析控制分支 SHA 并加载接收器。结果就绪后，串行 publish 作业复查任务身份、
回写 PR 并生成页面；失效任务只刷新 Dashboard。
`collect --task-id <ID>` 只回写指定任务，不带 task ID 时只汇总页面数据。

## 结果与页面发布

结果按事件、目标分支及源码提交保存：

- PR：`runs/pr/branch-<目标分支>/pr-<PR号>/<head_sha>/<run_id>/result.json`。
- push/manual：`runs/push/branch-<目标分支>/<head_sha>/<run_id>/result.json`。

分支名中的 `/` 使用 URL 编码，head SHA 为完整 40 位。
接收器按结果中的 task ID 匹配，不混用同一 SHA 的不同任务。
`result.json` 与重要文件一次提交，标题为
`local-ci: <status> <head_sha前12位> <run_id>`。

`result.json`、`change_validation` 报告及 `checks.evidence` 文件必传，
`artifacts` 按剩余预算选择补充文件。
必传证据无法发布时保留检查状态，整体通过改为 `infra_error`；
选传文件超限保留在主机并注明。数量和大小限制见
[结果与证据](../local_ci/README.md#结果与证据)。
上传响应丢失后重试相同内容，不重新运行 Agent。

页面发布比较 schema、tasks 和静态资源 `site_digest`，仅内容变化才部署 Pages。
部署由独立 `deploy-dashboard` 作业完成，`github-pages` environment 允许 `main`。
手动刷新页面使用 `main` 的 CI Gateway、`mode=publish`，不带 task ID。
PR 任务以 head SHA 为展示标识，tested SHA 保留在验证详情中。

PR 评论发送失败记录为 `receiver_error`，发布失败在接收轮次预算内重试。
页面发布与测试结果独立处理；Worker 通过 Gitee 通信，健康告警由
[Cloudflare](../local_ci/maintenance/cloudflare/README.md) 读取服务器快照。

## 配置

| 配置 | 用途 |
| --- | --- |
| `GITEE_RESULTS_REPO_URL` | 承载固定源码 refs、控制与结果的 Gitee HTTPS 仓库 |
| `GITEE_SUBMODULE_MIRRORS` | 随任务检出的子模块路径到 Gitee 镜像 URL 的 JSON 映射；无此类子模块时可省略 |
| `GITEE_USERNAME` / secret `GITEE_TOKEN` | 结果仓库 Git 读写权限 |
| `local-ci-fork-approval` environment | 外部 fork 审批，配置非空 required reviewers |

工作流按作业配置权限：状态回写使用 `statuses: write`，读取 Actions 与审批使用
`actions: read`，派发接续使用 `actions: write`，PR 评论使用 `pull-requests: write`。
接收和发布还按 YAML 配置检查读取权限，Pages 部署使用 `pages: write` 与 `id-token: write`。

PR 任务使用 `control_policy=worker`，在已安装的可信控制版本运行，
实际版本写入结果的 `environment.control_revision`。
push/manual 绑定精确控制提交，服务器按任务请求从 Gitee 快进更新。
各侧源码需要匹配的可信 profile，部署操作见 [服务器准备](../local_ci/prepare/README.md)。

本地回归：

```bash
python3 -m pytest scripts/local_ci/tests -q
```

测试使用本地 bare Git 和模拟 API，检查冻结身份、回写归属、审批及交付行为。
