# Local CI Gateway 与接收器

`ci-gateway.yml` 是需要与 `main` 路由保持兼容的稳定入口，展示名为 `CI Gateway`。它冻结 PR 的 base/head 和精确 merge SHA，执行前置检查，按需等待外部 fork 的 environment 审批，再复查 PR。源码和需要随任务检出的子模块固定 refs 全部推送到 Gitee 后，才用同一个控制提交发布不可变 `tasks/`、当前 `current/` 和取消 `cancel/`。源码 refs 含完整 task ID，重试不能移动另一任务的源码。可复用检查按功能命名为 `Local CI / Basic Checks`、`Local CI / API Compatibility` 和 `Local CI / Security`；上游巡检使用 `Maintenance / Upstream Triton Watch`。

根目录 `FlagGems` 使用服务器 profile 中预置的依赖，不要求网关提供镜像，也不随任务推送或检出；PR 修改 `FlagGems` 指针不会切换服务器固定依赖。其他子模块仍固定到被测 Git 对象。

取消旧任务按仓库和任务对象定位唯一 `current` 指针：PR 只处理该 PR，push/manual 只处理当前分支的非 PR 任务。`collect --task-id <ID>` 只向 GitHub 回写该任务；Dashboard 仍读取全部记录。不带任务 ID 的手工 `collect` 只汇总结果，不发评论、改状态或执行取消扫描。其他任务的展示错误不触发本次接收重试。

旧版 schema 的任务记录保留在 Gitee，网关的取消扫描与接收器识别后跳过，不让旧记录阻断新任务，也不据此回写通过；旧记录不列入新版看板当前任务列表。新版任务仍要求完整 task ID 对应的固定 refs，损坏记录不会被当成旧格式忽略。

`main` 的 `ci-gateway.yml` 保留路由和接收入口，完整执行流程位于 `local-ci-unified`。控制分支的 push 直接执行检查，使用本次提交 SHA，不再向自身派发；其他已有此入口的分支允许 push，并路由到控制分支的当前 SHA。已有非草稿 PR 对应同一提交时，由 PR 流程负责，避免重复投递。删除分支不投递任务。没有 workflow 入口的分支通过 PR 或手动 request 指定源码分支运行；所有 PR 目标分支均可派发。

两个分支的入口展示名统一为 `CI Gateway`，作业按职责命名。自动派发的运行标题例如 `PR #55 | h:079850a | dispatch`、`PR #55 | h:079850a m:adc03ab | receive 1`；接续保留同一任务身份并递增轮次。可选内部参数 `run_title` 只用于展示，不参与任务校验，不需配置仓库变量。

任务成功投递到 Gitee 后，网关才在 `main` 启动 `mode=receive`，没有定时触发。接收器按 `task_id` 等待，每分钟检查一次，每轮最多 5 小时 40 分钟，最多 3 轮；收到有效结果或任务失效立即结束，等不到结果才接续下一轮。轮数固定在代码中，不增加仓库变量。不同任务可以同时等待，接收不依赖新的服务器服务。

每轮先解析 `local-ci-unified` 的 SHA，再按该 SHA 加载接收器。结果就绪后由串行的 `publish` 作业复查任务身份、回写 PR 并生成 Dashboard；只有看板内容变化才上传 Pages 产物，由独立 `deploy-dashboard` 作业部署。发布时将静态资源相对路径与内容计算为 `site_digest`，与 schema、tasks 一起比较；资源新增、删除、重命名或内容变化会触发部署，生成时间变化不会触发重复部署。页面链接由部署步骤的 `page_url` 写入 environment URL，只影响后续部署记录。指纹不触发接收流程，静态资源更新在下一次 publish 时生效。`github-pages` 的审批或部署失败不阻塞 PR 结果回写，也不改变测试结论。发布失败仅在这三轮内补收，不重新执行构建；评论发送失败单独记录 `receiver_error`，不将已经发布的测试结论改写为环境错误。Worker 不连接 GitHub。健康采集和 watchdog 暂由 CI 主机上的独立定时器运行，不依赖 Gitee Go。

三轮耗尽后接收停止并报错，不取消服务器上的任务。需要继续收取已有任务时，在 Actions → CI Gateway → Run workflow 选择 `main`、`mode=receive`，填写任务摘要或 Gitee `tasks/` 文件名中的 `task_id`，`receiver_round` 保持 `1`。这只补收已有结果，不重新投递任务。

部署时配置：

| 配置 | 含义 |
| --- | --- |
| `GITEE_RESULTS_REPO_URL` | CI 专用 HTTPS Gitee 仓库，承载固定源码 refs、控制和小结果 |
| `GITEE_SUBMODULE_MIRRORS` | 除根目录 `FlagGems` 外，需要随任务检出的子模块路径到 Gitee 镜像 URL 的 JSON 映射；没有这类子模块时无需配置 |
| `GITEE_USERNAME` / secret `GITEE_TOKEN` | 该仓库的 Git 读写权限 |
| `local-ci-fork-approval` environment | 外部 fork 审批；必须实际保存非空 required reviewers |

无需配置控制分支、控制 SHA 或目标分支列表的 Actions 变量。服务器通过 `control_repo_url` / `control_branch` 从 Gitee 自动快进控制 checkout。新 PR 任务使用 `control_policy=worker`，由服务器当前已安装的可信控制代码执行，不要求与网关记录的 `worker_revision_sha` 一致，该字段也不再参与新 PR 的任务身份；实际控制版本记录在结果 `environment.control_revision`。旧格式任务身份保持兼容，push/manual 仍使用原有精确控制版本更新机制。各 PR 目标分支仍需在服务器配置对应环境 profile。封存结果可继续单独补传。接收派发到 main，github-pages environment 须允许 main 部署。

PR 分支保护应要求 `local-ci/basic`、`local-ci/api`、`local-ci/security`、`local-ci/approve`、`local-ci/dispatch`、`local-ci/summary`。前五项是 Check Runs，summary 是 commit status，统一发布到 `tested_sha`。部署时从必检项中移除 `local-ci/preflight`，增加 approve 和 dispatch；dispatch 在投递完成前保持未完成，防止同一提交的旧通过结果绕过本次流程。旧 head-only 前置结果仅在任务身份匹配且成功时复制到 tested_sha；缺失或失败的证据不能被补成通过。取消和失败不标为 skipped/neutral。PR 关闭或转为草稿时结束现有等待检查。

冻结新任务后创建 queued dispatch，并重置 Basic、API、Security 和 approve 的本轮记录；不再创建 preflight。控制分支自身 push 继续复用原生 Basic/API/Security 检查。信息通过后 Basic 开始，Basic 通过后 API 开始，API 通过后 Security 开始。approve 在前置检查期间显示等待前置条件，全部通过后才显示等待人工审批，Details 和检查说明均提供本次工作流审批入口；拒绝或审批校验失败显示 approve failure，取消显示 cancelled，不创建 summary。可信来源无需人工审批时明确显示通过原因。审批通过后 approve 成功，dispatch 开始向 Gitee 投递。源码和不可变任务成功发布后才在 tested_sha 首次创建 summary pending，再完成 dispatch；前置、审批、投递失败均由对应 Checks 和工作流报错，不向 PR head 新建 summary。投递成功但接收器启动失败时更新已有 summary，允许单独重试接收。

同 SHA 重新请求也创建新的分项 Check Runs，不复用上轮成功/失败结论。旧未完成记录结束为取消；已存在的旧 summary 标记为结果已被替代，成功投递后切换为本次等待状态。GitHub 历史记录不能删除，新的 SHA 使用新记录；页面排序和分支保护的 Expected 占位由 GitHub 控制。

GitHub Checks 的名称、结论、标题、状态说明，以及 `local-ci/summary` 的说明保持英文；审批卡、最终 PR 评论和 Agent 的面向维护者解释保持中文。旧任务取消只更新 Checks/status 与 Gitee 停止标记，不追加“Local CI 旧任务已取消”评论；历史评论不自动删除。

审批卡依次展示前置检查表、本次审批对应的固定版本（目标分支及 head/base/merge SHA）、本次改动概览和审批入口。卡片同时写入 PR 和工作流摘要。外部 fork 仍通过配置 required reviewers 的 `local-ci-fork-approval` 环境审批，审批后复查冻结身份。

PR 信息、Basic、API、Security 必须全部通过才显示审批卡；失败、取消、跳过或缺失结果均不显示卡片，也不进入人工审批或投递服务器任务。该条件同时由 workflow 和网关检查。失败详情继续同步英文 Checks/status；PR 信息缺失时保留带感谢、明确补充项和更新描述指引的中文提示，不重复发送准入结果汇总。历史评论不自动删除。

PR 结果评论以 `result + task_id + run_id + result_digest` 确定稳定事件标记，不依赖展示正文；同一结果在格式调整后也不重复发送。旧格式机器人结果评论通过相同的不可变报告 URL 查重，原文保留。新运行追加新评论。最终结果以中文展示，“PR 提交”与“合并后验证提交”分行显示，不展示 task_id/run_id；“查看审查详情”与“变更意图与审查结论”并列，折叠内容先简述检查与审查数量，再展示原有表格。未选/未执行项目保留在详情表中，不逐条放入“合入阻塞与重要限制”；真实阻塞项和完整报告链接保留。需要人工判断的发现显示报告中的风险等级；缺失等级显示“未标注”。

审批卡在全部前置检查通过后展示贡献者、来源仓库/分支、文件数/增删行数、关键改动位置和完整 diff 链接；不展示“贡献者说明”和“任务范围与审批边界”。采集前后复核冻结 PR 身份。

当前任务由最新 dispatch Check Run 的 task ID 和工作流运行链接确认；旧 preflight 仅用于已有任务的读取兼容，不再创建。旧流程不能回写新一轮检查，接收器还要求本轮 dispatch 已成功，防止同任务重跑时旧结果在审批前回写。summary 仅在派发成功后首次创建。GitHub 对 pending commit status 可显示 `Waiting for status to be reported`；必检项自身的 Expected 占位无法由发布代码隐藏。

部署此状态修复时，`main` 路由文件的 `receive` job 也须包含 `checks: read`，供接收器核对任务身份；其 `publish`/`deploy-dashboard` 拆分也须同步，因为 `mode=receive` 工作流在 main 运行。`local-ci-unified` 中的 prepare/finalize 使用 `checks: write`；finalize 只读 PR，不再需要写评论权限，审批验证使用 `checks: write` 回写 approve。仅更新控制分支不会自动改变 main 上的工作流定义。

Actions 的 `route`、`enqueue`、`receive`、`publish` 成功只表示相应调度或传输完成，不能替代 `local-ci/summary` 的测试结论。前置 checks 成功与服务器 `infra_error` 可以同时出现。对当前 PR head 上已有的机器人 summary，以及 head/tested 上已有的 `local-ci/sophgo-cmodel`，在身份有效且 canonical summary 状态匹配时追加与当前结论一致的状态，避免旧失败/pending 悬挂；后者明确标为 `Retired context; follows local-ci/summary`，不声称旧后端测试重新通过。没有旧 context 时不创建，不修改其他机器人状态，历史记录保留。PR 门禁应使用上面的六个当前 context；控制分支自身 push 的规则不能要求已省略的三个汇总。

结果按事件和目标分支保存。PR 为 `runs/pr/branch-<目标分支>/pr-<PR号>/<head_sha>/<run_id>/result.json`，push/manual 为 `runs/push/branch-<目标分支>/<head_sha>/<run_id>/result.json`；分支名中的 `/` 使用 URL 编码，head_sha 为完整 40 位提交号。所选日志与报告位于同目录的 `artifacts/`；一次 Git 提交同时发布结果和文件，标题为 `local-ci: <status> <head_sha前12位> <run_id>`。接收器按结果内部的 task_id 匹配，避免同一 SHA 的不同冻结任务串用结果；仍兼容读取原有分组 `<task_id>` 目录和 `runs/<task_id>/<run_id>/` 历史结果。单文件最多 2 MiB，合计最多 10 MiB，最多 20 个文件。超预算文件保留在 CI 主机并在结果中说明，不分片。上传响应丢失后重试相同提交内容，不产生重复结果或重新运行 Agent。

接收器不修改 Gitee 结果。旧版 schema 保留为历史，新任务使用无版本号的 `triton-anchor-local-ci-task` 与 `triton-anchor-local-ci`。常规任务由 Codex 根据实际 diff 选测，主机封存时要求 `change_validation` 说明影响、验证选择并引用实际证据；显式 full 仍要求全部可用工具覆盖。PR 信息和架构审查继续必需，缺失必需结果、审查或声称通过却不存在的证据文件均不能产生通过结果。

本地回归：`python3 -m pytest scripts/local_ci/tests -q`。测试使用本地 bare Git 与模拟 GitHub 边界，不发布真实结果。
