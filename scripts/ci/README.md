# Local CI Gateway 与接收器

`ci-gateway.yml` 是需要与 `main` 路由保持兼容的稳定入口，展示名为 `Local CI / Orchestrator`。它冻结 PR 的 base/head 和精确 merge SHA，执行前置检查，按需等待外部 fork 的 environment 审批，再复查 PR。源码和需要随任务检出的子模块固定 refs 全部推送到 Gitee 后，才用同一个控制提交发布不可变 `tasks/`、当前 `current/` 和取消 `cancel/`。源码 refs 含完整 task ID，重试不能移动另一任务的源码。可复用检查按功能命名为 `Local CI / Basic Checks`、`Local CI / API Compatibility` 和 `Local CI / Security`；上游巡检使用 `Maintenance / Upstream Triton Watch`。

根目录 `FlagGems` 使用服务器 profile 中预置的依赖，不要求网关提供镜像，也不随任务推送或检出；PR 修改 `FlagGems` 指针不会切换服务器固定依赖。其他子模块仍固定到被测 Git 对象。

旧版 schema 的任务记录保留在 Gitee，网关的取消扫描与接收器识别后跳过，不让旧记录阻断新任务，也不据此回写通过；旧记录不列入新版看板当前任务列表。新版任务仍要求完整 task ID 对应的固定 refs，损坏记录不会被当成旧格式忽略。

`main` 的 `ci-gateway.yml` 保留路由和接收入口，完整执行流程位于 `local-ci-unified`。每次派发自动读取该控制分支的当前 SHA，写入任务并用于执行校验；所有 PR 目标分支均可派发，push 自动触发仍限于 main。

任务成功投递到 Gitee 后，网关才在 `main` 启动 `mode=receive`，没有定时触发。接收器按 `task_id` 等待，每分钟检查一次，每轮最多 5 小时 40 分钟，最多 3 轮；收到有效结果或任务失效立即结束，等不到结果才接续下一轮。轮数固定在代码中，不增加仓库变量。不同任务可以同时等待，接收不依赖新的服务器服务。

每轮先解析 `local-ci-unified` 的 SHA，再按该 SHA 加载接收器。结果就绪后由串行的 `publish` 作业复查任务身份、回写 PR 并生成 Dashboard；只有看板内容变化才上传 Pages 产物，由独立 `deploy-dashboard` 作业部署。生成时间变化不会触发重复部署。`github-pages` 的审批或部署失败不阻塞 PR 结果回写，也不改变测试结论。发布失败仅在这三轮内补收，不重新执行构建；评论发送失败单独记录 `receiver_error`，不将已经发布的测试结论改写为环境错误。Worker 不连接 GitHub。健康采集和 watchdog 暂由 CI 主机上的独立定时器运行，不依赖 Gitee Go。

三轮耗尽后接收停止并报错，不取消服务器上的任务。需要继续收取已有任务时，在 Actions → Local CI / Orchestrator → Run workflow 选择 `main`、`mode=receive`，填写等待运行名称或 Gitee `tasks/` 文件名中的 `task_id`，`receiver_round` 保持 `1`。这只补收已有结果，不重新投递任务。

部署时配置：

| 配置 | 含义 |
| --- | --- |
| `GITEE_RESULTS_REPO_URL` | CI 专用 HTTPS Gitee 仓库，承载固定源码 refs、控制和小结果 |
| `GITEE_SUBMODULE_MIRRORS` | 除根目录 `FlagGems` 外，需要随任务检出的子模块路径到 Gitee 镜像 URL 的 JSON 映射；没有这类子模块时无需配置 |
| `GITEE_USERNAME` / secret `GITEE_TOKEN` | 该仓库的 Git 读写权限 |
| `local-ci-fork-approval` environment | 外部 fork 审批；必须实际保存非空 required reviewers |

无需配置控制分支、控制 SHA 或目标分支列表的 Actions 变量。服务器通过 `control_repo_url` / `control_branch` 从 Gitee 自动快进控制 checkout；Worker 要求任务中的控制 SHA 与服务器版本一致。各 PR 目标分支仍需在服务器配置对应环境 profile。封存结果可继续单独补传。接收派发到 main，github-pages environment 须允许 main 部署。

GitHub required checks 必须实际配置并查询验证：`local-ci/basic`、`local-ci/api`、`local-ci/security`、`local-ci/summary`。前三项是 PR head 上的 Check Runs，summary 是 commit status，避免同一名称同时对应两种门禁。真实失败使用 `failure`，取消使用 `cancelled`，被上游阻断或未执行使用 `action_required`；三者都不放行，且不将未执行误报成测试失败。不能用 `skipped` 或 `neutral` 表示未完成的必检，因为 GitHub 会将这两种结论视为满足 required check。PR 关闭或转为草稿时，尚在等待审批或执行的状态会结束为取消；网络失败只重试发布封存结果，不重新执行测试。

冻结新任务后立即将 summary 置为 pending，并为本任务排队三个前置 checks。Basic、API、Security 各阶段结束后，由独立的可信结果作业立即同步该阶段结论到 PR head 的 Checks，不等待其他检查或人工审批；实际开始时间受 GitHub runner 排队影响。结果作业只检出固定控制代码、校验任务摘要和当前身份，不检出或运行候选代码，也不改变 summary 和 PR 评论。审批卡与 finalizer 仍补齐最终状态，阶段回写失败时可重试。Check Run 按完整 task ID 更新，其他任务的已完成记录保留为历史；同一 head 上被新任务替代的旧等待检查结束为取消，不篡改旧失败结论。已完成任务的前置检查重跑会创建新记录。接收、取消和异常收尾在回写前核对最新前置 checks 的任务身份，防止新任务尚未投递到 Gitee 时，旧结果覆盖新门禁。审批拒绝、前置检查或投递失败由 `finalize-preflight` 结束等待状态并追加中文准入结果；已成功投递的任务继续由接收器负责 summary。

GitHub Checks 的名称、结论、标题、状态说明，以及 `local-ci/summary` 的说明保持英文；审批卡、最终 PR 评论和 Agent 的面向维护者解释保持中文。旧任务取消只更新 Checks/status 与 Gitee 停止标记，不追加“Local CI 旧任务已取消”评论；历史评论不自动删除。

审批卡与 `ci_repo` 的证据结构一致：前置检查表、完整 head/base/merge/control SHA、任务 ID、验证范围、审批原因和本次 Actions 证据/审批入口。卡片同时写入 PR 和工作流摘要；它只陈述已完成的前置检查，不预报服务器验证结果。外部 fork 仍以配置了 required reviewers 的 `local-ci-fork-approval` 环境为唯一人工准入门禁，审批后继续复查冻结身份。

PR 评论是追加式历史，不再使用全局 marker 查找并覆盖旧评论。任务 ID 与完整反馈内容共同确定事件标记；新提交、新运行、审批卡与最终结果各自追加，同一事件的发布重试只查重。旧格式评论原样保留。最终结果以中文展示结论、提交/运行身份、已执行检查与审查、未选/未执行范围、阻塞项和可用证据链接；机器状态枚举、工具 ID 和原始诊断保留原值，Agent 的解释与最终答复要求中文。

部署此状态修复时，`main` 路由文件的 `receive` job 也须包含 `checks: read`，供接收器核对任务身份；其 `publish`/`deploy-dashboard` 拆分也须同步，因为 `mode=receive` 工作流在 main 运行。`local-ci-unified` 中的 prepare/finalize 使用 `checks: write`，finalize 还需 `pull-requests: write` 追加准入结果，审批验证使用 `checks: read`。仅更新控制分支不会自动改变 main 上的工作流定义。

Actions 的 `route`、`enqueue`、`receive`、`publish` 成功只表示相应调度或传输完成，不能替代 `local-ci/summary` 的测试结论。前置 checks 成功与服务器 `infra_error` 可以同时出现。旧版 `local-ci/sophgo-cmodel` 等 commit status 会留在历史提交上；新流程不再写入它们，也不将历史错误改写为通过。仓库门禁应只要求上面的四个当前 context。

结果按事件和目标分支保存。PR 为 `runs/pr/branch-<目标分支>/pr-<PR号>/<task_id>/<run_id>/result.json`，push/manual 为 `runs/push/branch-<目标分支>/<task_id>/<run_id>/result.json`；分支名中的 `/` 使用 URL 编码。所选日志与报告位于同目录的 `artifacts/`；一次 Git 提交同时发布结果和文件。接收器仍兼容读取迁移前的 `runs/<task_id>/<run_id>/` 历史结果。单文件最多 2 MiB，合计最多 10 MiB，最多 20 个文件。超预算文件保留在 CI 主机并在结果中说明，不分片。上传响应丢失后重试相同提交内容，不产生重复结果或重新运行 Agent。

接收器不修改 Gitee 结果。旧版 schema 保留为历史，新任务使用无版本号的 `triton-anchor-local-ci-task` 与 `triton-anchor-local-ci`。常规任务由 Codex 根据实际 diff 选测，主机封存时要求 `change_validation` 说明影响、验证选择并引用实际证据；显式 full 仍要求全部可用工具覆盖。PR 信息和架构审查继续必需，缺失必需结果、审查或声称通过却不存在的证据文件均不能产生通过结果。

本地回归：`python3 -m pytest scripts/local_ci/tests -q`。测试使用本地 bare Git 与模拟 GitHub 边界，不发布真实结果。
