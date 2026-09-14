# 环境准备与部署

`prepare/` 管理 Rootless Docker 环境、每任务容器和服务安装。部署使用固定控制提交、镜像 digest、完整 LLVM revision 和服务器预置依赖。源码及控制更新经 Gitee 到达 CI 主机。

填写 `config.example.json` 或 `profiles/config.template.json` 中的实际路径、Gitee 仓库、镜像、依赖和模型配置。`control_repo_url` 指向服务器可访问的、无内嵌凭据的 Gitee `control_anchor` 镜像，`control_branch` 默认 `local-ci-unified`。`branch_profiles` 将目标分支映射到 `profiles`；目标分支与 profile 同名时可直接选择。所有 PR 目标分支都可使用，但每个目标需要对应的环境配置。

新服务器不需要先手工克隆 `control_anchor`。先由受信任的配置管理或制品通道投放本目录中的独立引导脚本、配置和私有凭据，并取得已经审核的 40 位控制提交 SHA。先预览，再以普通 CI 用户执行：

```bash
python3 bootstrap_control.py \
  --config /absolute/path/config.json \
  --credentials-env /absolute/path/credentials.env \
  --expected-revision <APPROVED_40_CHARACTER_SHA>

python3 bootstrap_control.py \
  --config /absolute/path/config.json \
  --credentials-env /absolute/path/credentials.env \
  --expected-revision <APPROVED_40_CHARACTER_SHA> --apply
```

引导脚本不会从可变分支下载后直接执行代码：它在写入前核对远端分支尖端，在浅克隆后再次核对固定 SHA，然后只调用该提交内的正式安装器。目标目录已经存在时，仅接受来源一致、无本地修改且恰好位于该 SHA 的 checkout，不覆盖未知目录。安装失败后保留固定 checkout，修复环境后可用同一命令幂等重试。引导脚本本身仍必须通过受信任通道分发，不能用 `curl <可变分支> | python` 代替。

安装器读取私有 `KEY=value` 凭据文件，值含空格时使用引号；文件必须属于 CI 用户且权限为 600。安装完成后，`triton-anchor-local-ci-control-update.timer` 每约五分钟检查 Gitee，只允许干净 checkout 快进，并在没有任务占用控制锁时切换版本、重启 Worker。非快进或本地修改会使更新失败并保留现场。所有 profile 使用顶层 `image` 指定的同一个镜像 digest，分支差异由只读依赖挂载和环境变量提供，不再构建派生镜像。安装器检查依赖与基础工具是否可用，实测 Rootless Docker 资源限制，然后安装并启动 Worker、control-update、health、retention 用户服务和定时器。watchdog 由 Gitee health 仓库的定时流水线执行；升级安装时会停用并删除旧的同机 watchdog units。已有 units 会备份，可用 `--rollback <备份目录> --apply` 恢复。机器需要已有的 Rootless Docker 用户服务和持久用户会话。

不加 `--apply` 输出安装计划；`--render-dir <目录>` 保存 units。`preflight.py --config <配置> --configuration-only` 可单独检查配置；`--probe-runtime` 实测已准备环境。可用 `control_update.py --config <配置>` 预览远端控制版本，带 `--apply` 执行同一安全更新。依赖更新时可运行 `rotate.py --config <配置> --profile <名称>`，登记并探测新的依赖环境，不构建镜像。环境准备不再执行完整 Wheel 构建、安装或 smoke；被测源码的验证在正式任务中完成，单独更新控制代码不会触发环境重校验。以上入口均支持 `--help`。

从旧配置升级时，将 profile 内的 `image` 合并为顶层一个 digest；LLVM 使用 `mode: mount`，原 `archives` / `repositories` 中的依赖改为预置的只读目录。删除旧 `prepare_commands` 与 `validation_commands`，镜像本身需要的安装步骤放在共享镜像配方中。示例配置列出了 LLVM、后端、PPL 和 FlagGems 的挂载位置。构建默认使用 12 路并行，已有配置的 `max_jobs` / `MAX_JOBS` 不会自动覆盖；按服务器资源设置，Codex 也可通过工具的 `jobs` 参数调整（1–64）。

每任务一个容器，Codex、构建和测试共用 `identities.task` / `identities.gid` 的非 root 身份。candidate、base 和临时实验是任务内的数据目录。可写挂载只有 `work/<task>/<run> → /task` 与 `runs/<task>/<run>/artifacts → /task/artifacts`；控制快照和 LLVM、FlagGems、后端等服务器依赖只读挂载。状态、私有日志和已封存结果留在宿主。

`runtime.py` 负责镜像及容器生命周期；`container_fs.py` 在容器内准备工作目录、独立 venv 和私有 Codex 会话，结束后清理凭据。任务命令可写自己的源码和构建输出；取消、超时和重启恢复由 Worker 停止对应任务，清理不删除已经保存的结果。

只读依赖的目录、权限和摘要配置见 [DEPENDENCY_MOUNTS.md](DEPENDENCY_MOUNTS.md)。`profiles/slim/` 提供共享基础镜像配方，FlagGems 从固定服务器目录导入，各任务的 venv 和构建输出独立。后端测试默认路径为 `tests`；多个路径可显式设置 profile 的 `tools.backend_test_paths` 数组。

健康采集、异常观察和本地保留策略见 [maintenance/README.md](../maintenance/README.md)。
