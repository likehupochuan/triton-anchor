# Local CI Gateway 与接收器

`ci-gateway.yml` 冻结 PR 的 base/head 和精确 merge SHA，执行前置检查，按需等待外部 fork 的 environment 审批，再复查 PR。源码和需要随任务检出的子模块固定 refs 全部推送到 Gitee 后，才用同一个控制提交发布不可变 `tasks/`、当前 `current/` 和取消 `cancel/`。源码 refs 含完整 task ID，重试不能移动另一任务的源码。

根目录 `FlagGems` 使用服务器 profile 中预置的依赖，不要求网关提供镜像，也不随任务推送或检出；PR 修改 `FlagGems` 指针不会切换服务器固定依赖。其他子模块仍固定到被测 Git 对象。

旧版 schema 的任务记录保留在 Gitee，网关的取消扫描与接收器识别后跳过，不让旧记录阻断新任务，也不据此回写通过；旧记录不列入新版看板当前任务列表。新版任务仍要求完整 task ID 对应的固定 refs，损坏记录不会被当成旧格式忽略。

`main` 的 `ci-gateway.yml` 保留路由和接收入口，完整执行流程位于 `local-ci-unified`。每次派发自动读取该控制分支的当前 SHA，写入任务并用于执行校验；所有 PR 目标分支均可派发，push 自动触发仍限于 main。

任务成功投递到 Gitee 后，网关才在 `main` 启动 `mode=receive`，没有定时触发。接收器按 `task_id` 等待，每分钟检查一次，每轮最多 5 小时 40 分钟，最多 3 轮；收到有效结果或任务失效立即结束，等不到结果才接续下一轮。轮数固定在代码中，不增加仓库变量。不同任务可以同时等待，接收不依赖新的服务器服务。

每轮先解析 `local-ci-unified` 的 SHA，再按该 SHA 加载接收器。结果就绪后由串行的 `publish` 作业复查任务身份、回写 PR 并生成 Dashboard，只有看板内容变化才部署 Pages；生成时间变化不会触发重复部署。发布失败也仅在这三轮内补收，不重新执行构建。Worker 不连接 GitHub。健康采集和 watchdog 仍在 CI 主机的 `scripts/local_ci/maintenance/` 独立运行。

三轮耗尽后接收停止并报错，不取消服务器上的任务。需要继续收取已有任务时，在 Actions → CI Gateway → Run workflow 选择 `main`、`mode=receive`，填写等待运行名称或 Gitee `tasks/` 文件名中的 `task_id`，`receiver_round` 保持 `1`。这只补收已有结果，不重新投递任务。

部署时配置：

| 配置 | 含义 |
| --- | --- |
| `GITEE_RESULTS_REPO_URL` | CI 专用 HTTPS Gitee 仓库，承载固定源码 refs、控制和小结果 |
| `GITEE_SUBMODULE_MIRRORS` | 除根目录 `FlagGems` 外，需要随任务检出的子模块路径到 Gitee 镜像 URL 的 JSON 映射；没有这类子模块时无需配置 |
| `GITEE_USERNAME` / secret `GITEE_TOKEN` | 该仓库的 Git 读写权限 |
| `local-ci-fork-approval` environment | 外部 fork 审批；必须实际保存非空 required reviewers |

无需配置控制分支、控制 SHA 或目标分支列表的 Actions 变量。控制版本升级时仍须同步服务器 checkout；Worker 要求任务中的控制 SHA 与服务器版本一致。各 PR 目标分支仍需在服务器配置对应环境 profile。封存结果可继续单独补传。接收派发到 main，github-pages environment 须允许 main 部署。

GitHub required checks 必须实际配置并查询验证：`local-ci/basic`、`local-ci/api`、`local-ci/security`、`local-ci/summary`。前三项是 PR head 上的 Check Runs，summary 是 commit status，避免同一名称同时对应两种门禁。跳过和取消的前置检查按失败门禁处理。PR 关闭或转为草稿时，尚在等待审批或执行的状态会结束为取消；网络失败只重试发布封存结果，不重新执行测试。

结果保存在 `runs/<task_id>/<run_id>/result.json`，所选日志与报告位于同目录的 `artifacts/`；一次 Git 提交同时发布结果和文件。单文件最多 2 MiB，合计最多 10 MiB，最多 20 个文件。超预算文件保留在 CI 主机并在结果中说明，不分片。上传响应丢失后重试相同提交内容，不产生重复结果或重新运行 Agent。

接收器不修改 Gitee 结果。旧版 schema 保留为历史，新任务使用无版本号的 `triton-anchor-local-ci-task` 与 `triton-anchor-local-ci`。最低必检、PR 信息和架构审查在主机封存时汇总；缺失检查、审查或声称通过却不存在的证据文件均不能产生通过结果。

本地回归：`python3 -m pytest scripts/local_ci/tests -q`。测试使用本地 bare Git 与模拟 GitHub 边界，不发布真实结果。
