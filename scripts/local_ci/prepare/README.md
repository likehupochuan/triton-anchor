# 环境准备与部署

`prepare/` 管理 Rootless Docker 环境、每任务容器和服务安装。部署使用固定控制提交、镜像 digest、完整 LLVM revision 和服务器预置依赖。源码及控制更新经 Gitee 到达 CI 主机。

填写 `config.example.json` 或 `profiles/config.template.json` 中的实际路径、Gitee 仓库、镜像、依赖和模型配置。`control_repo_url` 指向服务器可访问的、无内嵌凭据的 Gitee `control_anchor` 镜像，`control_branch` 默认 `local-ci-unified`。环境选择优先使用 `branch_profiles` 显式映射，其次使用与目标分支同名的 profile；两者都没有时，按任务的 LLVM SHA 唯一匹配已配置 profile 的 `llvm_hash` 或 `llvm.revisions`，复用其镜像和依赖。该 SHA 由网关读取被测提交的 LLVM 元数据，服务器再对照 Gitee 源码校验。无匹配时需要部署对应环境；多个 profile 支持同一 SHA 时才需显式映射，不按分支名称猜测 Triton 版本。已有显式选择不会因 LLVM 不匹配而悄悄换成其他环境。

网关和服务器共用 LLVM 元数据解析：只在被测提交的 `triton/cmake/` 目录识别 `llvm-hash`、`llvm-info`，兼容无扩展名、`.txt` 和 `.json`。纯文本应为完整的 40 位提交 SHA，JSON 读取 `llvm_hash` 字段。多个文件同时存在时必须给出相同 SHA；不读取 `amd-llvm-info.json` 等其他后端元数据，也不从构建脚本中搜索任意哈希。

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

安装器读取私有 `KEY=value` 凭据文件，值含空格时使用引号；文件必须属于 CI 用户且权限为 600。Worker 发现新任务要求不同的控制提交时，释放共享控制锁，将任务身份和 SHA 原子写入单一 `control-update/request.json`，再异步启动 `triton-anchor-local-ci-control-update.service`；多个等待版本按 `control_anchor` 的提交祖先顺序选最早的前向版本，落后或不可达版本不会阻塞可用的前向更新。该 oneshot 只允许干净 checkout 快进到任务指定且可从 Gitee 控制分支到达的提交，随后重启 Worker，成功后清除仍与本次任务一致的请求。Worker 以进程启动时的提交为准，不会把已经变化的磁盘 HEAD 误认为当前代码；重启后的 Worker 会幂等清理已满足但上次未及删除的请求。没有新任务时不检查控制更新，非快进、本地修改或尚未同步到 Gitee 的提交会安全失败并由后续扫描重试。所有 profile 使用顶层 `image` 指定的同一个镜像 digest，分支差异由只读依赖挂载和环境变量提供，不再构建派生镜像。安装器检查依赖与基础工具是否可用，实测 Rootless Docker 资源限制，然后安装并启动 Worker、control-update oneshot、health、watchdog、retention 用户服务和定时器；升级时会停用、备份并删除旧的 control-update timer。watchdog 暂由 CI 主机上的独立 timer 运行，不依赖 Gitee Go；同机停机时无法发出离线告警。已有 unit 文件会备份，可用 `--rollback <备份目录> --apply` 恢复文件；systemd 的 enabled/active 状态需按回滚目标另行恢复。机器需要已有的 Rootless Docker 用户服务和持久用户会话。

不加 `--apply` 输出安装计划；`--render-dir <目录>` 保存 units。`preflight.py --config <配置> --configuration-only` 可单独检查配置；`--probe-runtime` 实测已准备环境。可用 `control_update.py --config <配置>` 预览远端控制版本；手工应用时必须同时给出 `--expected-revision <40位SHA> --apply`，正式服务则只读取固定位置的请求文件，不允许无精确 SHA 更新。依赖更新时可运行 `rotate.py --config <配置> --profile <名称>`，登记并探测新的依赖环境，不构建镜像。环境准备不再执行完整 Wheel 构建、安装或 smoke；被测源码的验证在正式任务中完成，单独更新控制代码不会触发环境重校验。以上入口均支持 `--help`。

从旧配置升级时，将 profile 内的 `image` 合并为顶层一个 digest；LLVM 使用 `mode: mount`，原 `archives` / `repositories` 中的依赖改为预置的只读目录。删除旧 `prepare_commands` 与 `validation_commands`，镜像本身需要的安装步骤放在共享镜像配方中。示例配置列出了 LLVM、后端、PPL 和 FlagGems 的挂载位置。构建默认使用 12 路并行，已有配置的 `max_jobs` / `MAX_JOBS` 不会自动覆盖；按服务器资源设置，Codex 也可通过工具的 `jobs` 参数调整（1–64）。

每任务一个容器，Codex、构建和测试共用 `identities.task` / `identities.gid` 的非 root 身份。candidate、base 和临时实验是任务内的数据目录。可写挂载只有 `work/<head_sha>/<run_id> → /task` 与当前运行目录的 `artifacts → /task/artifacts`；运行目录按 [本地与 Gitee 共用的命名规则](../README.md) 分层。任务结束后删除临时运行目录及空的 SHA 父目录，保留其他运行和持久化证据；旧 task_id 工作目录按原句柄安全清理。直接将 `control_root`（服务器上的 `control_anchor`）中的 `scripts`、`api_contract` 和 `envsetup.sh` 只读挂载到容器 `/opt/local-ci/control/` 下的对应位置，不再导出 `environments/control-revisions/<SHA>` 快照。LLVM、FlagGems、后端等服务器依赖仍只读挂载；`.git`、凭据、状态、私有日志和已封存结果留在宿主，不能放入上述挂载目录。

Worker 接单仍校验控制 SHA，并在整个任务期间持有现有 `control.lock`。自动更新拿不到独占锁，或发现本实例尚有任务容器或清理容器未移除时，会延后切换 checkout。挂载前仅将受版本控制的运行文件和目录设为容器可读，兼容服务的 `UMask=0077`。手动更新 `control_anchor` 时，须先停止 Worker、确认任务容器已退出并清理，再更新和启动 Worker。旧任务的快照仍可用于恢复清理，但新任务不再创建快照。

`runtime.py` 负责镜像及容器生命周期；`container_fs.py` 在容器内准备工作目录、独立 venv 和私有 Codex 会话，结束后清理凭据。任务命令可写自己的源码和构建输出；取消、超时和重启恢复由 Worker 停止对应任务，清理不删除已经保存的结果。

只读依赖的目录、权限和摘要配置见 [DEPENDENCY_MOUNTS.md](DEPENDENCY_MOUNTS.md)。`profiles/slim/` 提供共享基础镜像配方，FlagGems 从固定服务器目录导入，各任务的 venv 和构建输出独立。后端测试默认路径为 `tests`；多个路径可显式设置 profile 的 `tools.backend_test_paths` 数组。

健康采集、异常观察和本地保留策略见 [maintenance/README.md](../maintenance/README.md)。

## 容器内 CI Python

`container_python` 必须指向镜像中专门准备的 CI 虚拟环境（通常为 `/opt/venv/bin/python`），
不能配置成 `/usr/bin/python3` 或仅 `python3`。未显式配置时使用 Profile 的 `SEED_PYTHON`
或 `PYTHON_VENV_ACTIVATE` 对应解释器；它用于可信管理操作，并为 candidate/base 各自生成
可写的任务 venv。Codex 的构建、安装和测试使用相应任务 venv，环境修复不会污染预置环境。
任务 venv 直接复制预置包（包括 pip），不依赖系统 `ensurepip`；安装依赖使用 `"$PYTHON_BIN" -m pip`。
宿主机服务的 `python_bin` 与容器解释器独立，仍可使用宿主机 Python。
