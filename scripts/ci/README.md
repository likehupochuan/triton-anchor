# Local CI Gateway 与接收器

默认分支 `main` 上的 `ci-request.yml` 是唯一 PR 请求入口，展示名为 `CI Request`，只包含 `Route CI request` 一个 job；`main` 的 `ci-gateway.yml` 只负责 receive、publish 和 Dashboard，`local-ci-unified` 的同名文件只负责 prepare、Checks、审批和 enqueue。执行侧网关冻结 PR 的 base/head 和精确 merge SHA，执行前置检查，按需等待外部 fork 的 environment 审批，再复查 PR。源码和需要随任务检出的子模块固定 refs 全部推送到 Gitee 后，才用同一个控制提交发布不可变 `tasks/`、当前 `current/` 和取消 `cancel/`。源码 refs 含完整 task ID，重试不能移动另一任务的源码。GitHub 侧三项前置 Check Run 上报为 `github/basic`、`github/api`、`github/security`；`local-ci/*` 仅用于投递、审批和最终 Local CI 结果。上游巡检使用 `Maintenance / Upstream Triton Watch`。

根目录 `FlagGems` 使用服务器 profile 中预置的依赖，不要求网关提供镜像，也不随任务推送或检出；PR 修改 `FlagGems` 指针不会切换服务器固定依赖。其他子模块仍固定到被测 Git 对象。

取消旧任务按仓库和任务对象定位唯一 `current` 指针：PR 只处理该 PR，push/manual 只处理当前分支的非 PR 任务。`collect --task-id <ID>` 只向 GitHub 回写该任务；Dashboard 仍读取全部记录。不带任务 ID 的手工 `collect` 只汇总结果，不发评论、改状态或执行取消扫描。其他任务的展示错误不触发本次接收重试。

旧版 schema 的任务记录保留在 Gitee，网关的取消扫描与接收器识别后跳过，不让旧记录阻断新任务，也不据此回写通过；旧记录不列入新版看板当前任务列表。新版任务仍要求完整 task ID 对应的固定 refs，损坏记录不会被当成旧格式忽略。

`main` 的 `ci-request.yml` 监听 `pull_request_target`，不向其他 PR 目标分支同步。它不包含接收、发布和 Dashboard 部署作业，因此不会为这些不适用阶段生成 skipped Check Runs。当前合入前阶段把 `mode=run` 调度到 `local-ci-unified`；`main` 的 `ci-gateway.yml` 继续负责 receive、publish 和 Dashboard，完整执行流程暂时位于 `local-ci-unified`。未来完整 Gateway 合入 `main` 时，只需将请求入口中的 `controlRef` 从 `local-ci-unified` 改为 `main`，无需增加仓库变量。控制分支的 push 直接执行检查，使用本次提交 SHA，不再向自身派发；已有非草稿 PR 对应同一提交时，由 PR 流程负责，避免重复投递。删除分支不投递任务。

请求入口展示名为 `CI Request`，控制执行与接收入口展示名为 `CI Gateway`，作业按职责命名。自动派发的运行标题例如 `PR #55 | h:079850a | dispatch`、`PR #55 | h:079850a m:adc03ab | receive 1`；接续保留同一任务身份并递增轮次。可选内部参数 `run_title` 只用于展示，不参与任务校验，不需配置仓库变量。

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

PR 分支保护应要求 `github/basic`、`github/api`、`github/security`、`local-ci/dispatch`、`local-ci/summary`。这些是当前任务的必检上下文，前三项和 dispatch 是 Check Runs，summary 是 commit status；PR 任务统一发布到贡献者 `head_sha`，push/manual 任务发布到 `tested_sha`。`local-ci/approve` 只在外部 fork 需要审批时临时回写，不作为所有 PR 的 required context；dispatch 由审批结果门控，因此仍能表达外部审批是否完成。部署时从必检项中移除 `local-ci/preflight`。dispatch 在投递完成前保持未完成，防止同一提交的旧通过结果绕过本次流程。旧 merge-only 前置结果仅在任务身份匹配且成功时复制到 PR head；缺失或失败的证据不能被补成通过。实际执行仍冻结并验证 merge/tested SHA，证据链接也继续指向该被测提交。取消和失败不标为 skipped/neutral。PR 关闭或转为草稿时结束现有等待检查。

冻结新任务后只创建 Basic，先检查 PR 信息，再运行 Basic CI；Basic 通过后才创建 API，API 通过后才创建 Security。全部前置通过后，同仓库任务直接进入 dispatch；外部 fork 才发布审批卡并等待 approve，审批通过后才进入 dispatch。源码和不可变任务成功发布到 Gitee 后才首次创建 summary pending，等待服务器结果。审批拒绝或校验失败显示 approve failure，取消显示 cancelled；不创建 Dispatch 或 Summary。前置失败只结束已经到达的检查，不补建后续阶段。投递成功但接收器启动失败时更新已有 summary，允许单独重试接收。Basic → API → Security 的执行只依赖上游检查成功；basic-result、api-result 与下一检查并行回写，回写失败或排队不阻塞下一检查。审批卡（仅外部 fork）等待全部检查及回写作业成功；页面状态可能因回写延迟短暂落后于实际执行。

控制分支自身 push 继续使用原生 Basic/API/Security，不增加重复汇总或提前创建后续检查。普通任务以 Basic Check Run 为当前任务起点；控制分支自身 push 复用已有原生 `Prepare exact task`，通过 GitHub 运行信息确认来源。PR reopen、同 SHA 重试或追加提交时，网关复用当前上下文的最新自定义 Check Run 并刷新状态；同任务遗留的旧等待检查会结束。已存在的旧 summary 标记为结果已被替代，成功投递后切换为本次等待状态。GitHub 历史记录不能删除，页面排列、原生工作流作业和分支保护的 Expected 占位由 GitHub 控制；逐阶段规则控制的是额外回写的 `github/*` Check Runs。

GitHub Checks 的名称、结论、失败标题、状态说明，以及 `local-ci/summary` 的说明保持英文；成功的 `github/basic`、`github/api`、`github/security` 不再重复显示 `github/…: success` 标题。审批卡、最终 PR 评论和 Agent 的面向维护者解释保持中文。只有 `local-ci-fork-approval` 环境留下明确拒绝记录时，finalize 才在仍匹配的 PR 上 @贡献者并追加“进入 Local CI 审批未通过”通知；通知包含对应 PR 提交，审核者和批注存在时一并展示，并提示贡献者可联系审核者。审批作业校验失败或取消不伪装成人工拒绝，也不发送拒绝通知。旧任务取消只更新 Checks/status 与 Gitee 停止标记，不追加“Local CI 旧任务已取消”评论；历史评论不自动删除。

审批卡依次展示前置检查表、本次审批对应的固定版本（目标分支及 head/base/merge SHA）、本次改动概览和审批入口，仅对外部 fork 发布。卡片同时写入 PR 和工作流摘要。外部 fork 仍通过配置 required reviewers 的 `local-ci-fork-approval` 环境审批，审批后复查冻结身份；同仓库任务不进入卡片步骤。

PR 信息、Basic、API、Security 必须全部通过才允许外部 fork 显示审批卡；失败、取消、跳过或缺失结果均不显示卡片，也不进入人工审批或投递服务器任务。同仓库任务不执行审批卡步骤，直接进入 dispatch。该条件同时由 workflow 和网关检查。失败详情继续同步英文 Checks/status；PR 信息缺失时保留带感谢、明确补充项和更新描述指引的中文提示，不重复发送准入结果汇总。历史评论不自动删除。

PR 结果评论以 `result + task_id + run_id + result_digest` 确定稳定事件标记，不依赖展示正文；同一结果在格式调整后也不重复发送。旧格式机器人结果评论通过相同的不可变报告 URL 查重，原文保留。新运行追加新评论。最终结果以中文展示，“PR 提交”与“合并后验证提交”分行显示，不展示 task_id/run_id；PR 对应的 Dashboard 链接放在“查看审查详情”模块的检查表格下方，便于继续查看完整详情。该模块只列出实际执行的检查与审查；未选择、未执行和不适用项目及其原因仍完整保留在 Dashboard，不在评论中逐条展开。真实阻塞项和完整报告链接保留。需要人工判断的发现显示报告中的风险等级；缺失等级显示“未标注”。

审批卡在全部前置检查通过后展示贡献者、来源仓库/分支、文件数/增删行数、按文件路径归类的简短范围统计和完整 diff 链接；分类按“数量 + 类别 + 文件”展示，例如“1 个测试文件”。不逐一列出文件，不展示“贡献者说明”“任务范围与审批边界”或笼统的“审批关注”。分类仅帮助定位改动范围，不代表内容审查或风险结论。采集前后复核冻结 PR 身份。

当前任务由 Basic 或原生任务准备检查的 ID 和工作流运行 ID 确认。自定义 Check 的运行 ID 保存于说明中的隐藏标记，不使用 GitHub 会改写的 `details_url` 判断归属；PR reopen、同 SHA 重试会刷新当前上下文的最新 Check Run，避免重复创建同名历史行，同时仍以工作流运行 ID 拦截迟到的旧发布。旧 preflight/dispatch 仅用于已有任务的读取兼容；旧控制分支自身 push 的 Dispatch 若缺少运行标记，需重新 request，不能通过详情链接猜测归属。旧流程不能回写新一轮检查，接收器还要求 Dispatch 属于本轮且已成功，防止同任务重跑时旧结果在审批前回写。summary 仅在派发成功后首次创建。GitHub 对 pending commit status 可显示 `Waiting for status to be reported`；必检项自身的 Expected 占位无法由发布代码隐藏。

部署此状态修复时，只需在默认分支 `main` 部署 `ci-request.yml`，并让 `main` 的 `ci-gateway.yml` 不再监听 `pull_request_target`；不要求向其他目标分支同步请求入口。`main` 的 `ci-gateway.yml` 中 `receive` job 还须包含 `checks: read`，供接收器核对任务身份；其 `publish`/`deploy-dashboard` 拆分也须保留，因为 `mode=receive` 工作流在 main 运行。`local-ci-unified` 中查询原生任务起点的检查回写作业还需 `actions: read`，prepare/finalize 使用 `checks: write`；finalize 还需 `actions: read` 和 `pull-requests: write`，分别用于读取本次运行的审批历史、在明确拒绝时通知贡献者；审批验证使用 `checks: write` 回写 approve。

Actions 的 `route`、`enqueue`、`receive`、`publish` 成功只表示相应调度或传输完成，不能替代 `local-ci/summary` 的测试结论。前置 checks 成功与服务器 `infra_error` 可以同时出现。旧版 `local-ci/basic`、`local-ci/api`、`local-ci/security` 以及 sophgo-cmodel 变体不再创建、认领或回写；仍处于等待中的精确旧 Check Run 会在当前任务启动/PR 关闭时标记取消，已完成的 GitHub 历史行不能通过 API 删除。`local-ci/summary` 是 Commit Status，API 对每次 POST 追加历史记录；PR 当前实现只写 `head_sha`，push/manual 只写 `tested_sha`，同一 task/state/description 即使证据 URL 改变也不重复追加。状态真正转换时仍会保留历史，最新状态才是有效结论。PR 门禁应使用上面的五个当前 context；控制分支自身 push 的规则不能要求已省略的三个汇总。拆分请求入口后，新 PR 事件不会再生成不适用的 skipped 编排作业；已经生成的 GitHub 历史行仍无法删除。

Dashboard 的 PR 任务列表、搜索和任务详情以贡献者的 `head_sha` 作为主要提交标识；merge/tested SHA 仍保留在“被测提交与影响文件”中说明实际验证对象。非 PR 任务继续显示 `tested_sha`。

结果按事件和目标分支保存。PR 为 `runs/pr/branch-<目标分支>/pr-<PR号>/<head_sha>/<run_id>/result.json`，push/manual 为 `runs/push/branch-<目标分支>/<head_sha>/<run_id>/result.json`；分支名中的 `/` 使用 URL 编码，head_sha 为完整 40 位提交号。`result.json` 使用缩进 JSON 展示，不改变结果字段。该文件及 `checks.evidence` 引用的检查证据必须发布；任务通过 `artifacts` 选择的补充文件按剩余预算发布。所选日志与报告位于同目录的 `artifacts/`；一次 Git 提交同时发布结果和文件，标题为 `local-ci: <status> <head_sha前12位> <run_id>`。接收器按结果内部的 task_id 匹配，避免同一 SHA 的不同冻结任务串用结果；仍兼容读取原有分组 `<task_id>` 目录和 `runs/<task_id>/<run_id>/` 历史结果。单文件最多 2 MiB，合计最多 10 MiB，最多 20 个文件。必传证据无法发布时结果不能保持通过；选传文件超预算时保留在 CI 主机并在结果中说明，不分片。上传响应丢失后重试相同提交内容，不产生重复结果或重新运行 Agent。

接收器不修改 Gitee 结果。旧版 schema 保留为历史，新任务使用无版本号的 `triton-anchor-local-ci-task` 与 `triton-anchor-local-ci`。常规任务由 Codex 根据实际 diff 选测，主机封存时要求 `change_validation` 说明影响、验证选择并引用实际证据；显式 full 仍要求全部可用工具覆盖。PR 信息和架构审查继续必需，缺失必需结果、审查或声称通过却不存在的证据文件均不能产生通过结果。

本地回归：`python3 -m pytest scripts/local_ci/tests -q`。测试使用本地 bare Git 与模拟 GitHub 边界，不发布真实结果。
