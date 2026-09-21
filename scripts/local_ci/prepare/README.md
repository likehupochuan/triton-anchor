# 环境准备与部署

`prepare/` 管理 Rootless Docker 环境、每任务容器和服务安装。部署使用固定控制提交、镜像 digest、完整 LLVM revision 和服务器预置依赖。源码及控制更新经 Gitee 到达 CI 主机。

`config.example.json` 保留原名，但现在是 `jiwang_ci` 服务器完整非敏感部署配置的唯一维护来源，修改它会影响部署。请在开发仓库修改并提交，经 Gitee 控制更新流程部署；不要直接修改服务器控制 checkout 或运行副本。`/home/jiwang_ci/local_ci/config/local-ci.json` 由部署流程生成，没有本地覆盖 JSON。凭据仍独立保存在 `credentials.env` 与 `codex-source/`，配置中只记录路径或环境变量名；`profiles/config.template.json` 仅供其他部署参考，不参与此服务器配置同步。

`control_repo_url` 指向服务器可访问的、无内嵌凭据的 Gitee `control_anchor` 镜像，`control_branch` 默认 `local-ci-unified`。网关分别从冻结的 base 和 candidate 源码读取 LLVM SHA 与 Triton 版本，服务器对照 Gitee 源码校验，并按每一侧的 LLVM SHA 和 Triton major.minor 唯一选择可信 profile；不再使用分支名或 `branch_profiles` 路由，遗留的非空映射需删除。无匹配或存在多个匹配时明确报告环境配置问题，不借用另一侧环境。3.0 与 3.1 虽使用相同 LLVM，也分别选择 profile，避免把仅支持 3.0 的后端能力带入 3.1。

网关和服务器共用 LLVM 元数据解析：只在被测提交的 `triton/cmake/` 目录识别 `llvm-hash`、`llvm-info`，兼容无扩展名、`.txt` 和 `.json`。纯文本应为完整的 40 位提交 SHA，JSON 读取 `llvm_hash` 字段。多个文件同时存在时必须给出相同 SHA；不读取 `amd-llvm-info.json`、`llvm-build-info.json` 等其他元数据，也不从构建脚本中搜索任意哈希。Triton 3.8 使用 `llvm-info.json`，沿用同一解析逻辑。

新服务器不需要先手工克隆 `control_anchor`。先由受信任的配置管理或制品通道投放本目录中的独立引导脚本、该提交的 `config.example.json` 内容和私有凭据，并取得已经审核的 40 位控制提交 SHA。引导输入是同一仓库配置的分发副本，不单独维护。先预览，再以普通 CI 用户执行：

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

安装器读取私有 `KEY=value` 凭据文件，值含空格时使用引号；文件必须属于 CI 用户且权限为 600。Worker 发现新任务要求不同的控制提交时，释放共享控制锁，将任务身份和 SHA 原子写入单一 `control-update/request.json`，再异步启动 `triton-anchor-local-ci-control-update.service`；多个等待版本按 `control_anchor` 的提交祖先顺序选最早的前向版本，落后或不可达版本不会阻塞可用的前向更新。该 oneshot 只允许干净 checkout 快进到任务指定且可从 Gitee 控制分支到达的提交，并在重启 Worker 前同步该目标提交中的配置，复用一次重启加载新代码和新配置；成功后清除仍与本次任务一致的请求。Worker 以进程启动时的提交为准，不会把已经变化的磁盘 HEAD 误认为当前代码；重启后的 Worker 会幂等清理已满足但上次未及删除的请求。没有新任务时不检查控制更新，不定时追随分支最新提交；非快进、本地修改或尚未同步到 Gitee 的提交会安全失败并由后续扫描重试。所有 profile 使用顶层 `image` 指定的同一个镜像 digest，各源码版本的环境差异由只读依赖挂载和环境变量提供，不再构建派生镜像。安装器检查依赖与基础工具是否可用，实测 Rootless Docker 资源限制，然后安装并启动 Worker、control-update oneshot、health、retention 用户服务和定时器；升级时会停用、备份并删除旧的 control-update timer。不再安装同机 watchdog；安装与控制更新使用同一精确清理逻辑停用旧 watchdog service/timer，并刷新 systemd。首次由旧更新器升级到新代码后，须执行新版安装器 --apply 完成 unit 迁移。外部告警由 Cloudflare 读取健康快照完成。已有 unit 文件会备份，可用 `--rollback <备份目录> --apply` 恢复文件；systemd 的 enabled/active 状态需按回滚目标另行恢复。机器需要已有的 Rootless Docker 用户服务和持久用户会话。

已有控制 checkout 时，以 `jiwang_ci` 用户运行下面命令预览安装；加 `--apply` 才写入配置、安装和启动服务。运行配置尚不存在时也由安装器创建，`--config` 指定生成副本的位置：

```bash
python3 scripts/local_ci/prepare/install.py \
  --config /home/jiwang_ci/local_ci/config/local-ci.json \
  --credentials-env /home/jiwang_ci/local_ci/config/credentials.env
```

安装和控制更新共用配置同步实现，按 JSON 结构比较，不按时间戳判断；内容相同则跳过写入，有差异时先校验，再通过同目录临时文件原子替换，保持 `jiwang_ci` 所有和 600 权限。`control_root`、`state_dir`、`python_bin`、`runtime` 等宿主部署锚点变化需要重新运行安装器，控制更新不会热切换这些设置。

可显式预览指定目标提交的代码更新及配置差异；加 `--apply` 应用，同 SHA 也可修复配置偏差：

```bash
python3 scripts/local_ci/prepare/control_update.py \
  --config /home/jiwang_ci/local_ci/config/local-ci.json \
  --expected-revision <APPROVED_40_CHARACTER_SHA>
```

首次从旧更新器迁移时，先按现有流程到达包含同步功能的新提交，再用新脚本对当前同一 SHA 执行上述命令并加 `--apply`，完成首次同步及 Worker 重启。正式服务仍只读取固定位置的请求文件，不允许无精确 SHA 更新。

`--render-dir <目录>` 保存安装 units。`preflight.py --config <运行配置> --configuration-only` 可单独检查配置；`--probe-runtime` 实测已准备环境。依赖更新时可运行 `rotate.py --config <运行配置> --profile <名称>`，登记并探测新的依赖环境，不构建镜像。环境准备不再执行完整 Wheel 构建、安装或 smoke；被测源码的验证在正式任务中完成，单独更新控制代码不会触发环境重校验。以上入口均支持 `--help`。

从旧配置升级时，将 profile 内的 `image` 合并为顶层一个 digest；LLVM 使用 `mode: mount`，原 `archives` / `repositories` 中的依赖改为预置的只读目录。删除旧 `prepare_commands` 与 `validation_commands`，镜像本身需要的安装步骤放在共享镜像配方中。仓库部署配置列出了实际 LLVM、后端、PPL 和 FlagGems 挂载位置。构建默认使用 12 路并行，`max_jobs` / `MAX_JOBS` 按仓库配置部署；按服务器资源设置，Codex 也可通过工具的 `jobs` 参数调整（1–64）。

每任务一个容器，Codex、构建和测试共用 `identities.task` / `identities.gid` 的非 root 身份。candidate、base 和临时实验是任务内的数据目录。base 与 candidate 分别生成 context，记录各自 LLVM、profile、环境变量、后端能力和 fingerprint；各自的 checkout、venv、build、cache 与 artifacts 独立。相同 LLVM/profile 复用只读依赖，不复用可写构建结果；不同 LLVM 的只读版本目录可同时挂载到同一任务容器。可写挂载只有 `work/<head_sha>/<run_id> → /task` 与当前运行目录的 `artifacts → /task/artifacts`；运行目录按 [本地与 Gitee 共用的命名规则](../README.md) 分层。任务结束后删除临时运行目录及空的 SHA 父目录，保留其他运行和持久化证据；旧 task_id 工作目录按原句柄安全清理。直接将 `control_root`（服务器上的 `control_anchor`）中的 `scripts`、`api_contract` 和 `envsetup.sh` 只读挂载到容器 `/opt/local-ci/control/` 下的对应位置，不再导出 `environments/control-revisions/<SHA>` 快照。LLVM、FlagGems、后端等服务器依赖仍只读挂载；`.git`、凭据、状态、私有日志和已封存结果留在宿主，不能放入上述挂载目录。

新 PR 任务的 `control_policy=worker` 表示使用服务器已安装的可信控制版本，不因网关记录的控制 SHA 不同而等待升级；结果记录实际 `control_revision`。push/manual 和旧格式固定版本任务仍校验控制 SHA。所有任务均检查进程与磁盘版本一致，并在整个任务期间持有现有 `control.lock`。自动更新拿不到独占锁，或发现本实例尚有任务容器或清理容器未移除时，会延后切换 checkout。挂载前仅将受版本控制的运行文件和目录设为容器可读，兼容服务的 `UMask=0077`。手动更新 `control_anchor` 时，须先停止 Worker、确认任务容器已退出并清理，再更新和启动 Worker。旧任务的快照仍可用于恢复清理，但新任务不再创建快照。

`runtime.py` 负责镜像及容器生命周期；`container_fs.py` 在容器内准备工作目录、独立 venv 和私有 Codex 会话，结束后清理凭据。任务命令可写自己的源码和构建输出；取消、超时和重启恢复由 Worker 停止对应任务，清理不删除已经保存的结果。

只读依赖的目录、权限和摘要配置见 [DEPENDENCY_MOUNTS.md](DEPENDENCY_MOUNTS.md)。`profiles/slim/` 提供共享基础镜像配方，FlagGems 从固定服务器目录导入，各任务的 venv 和构建输出独立。后端测试默认路径为 `tests`；多个路径可显式设置 profile 的 `tools.backend_test_paths` 数组。

完整版本清单、尚待登记的服务器 LLVM 目录与摘要、配置补齐和跨版本验收步骤见 [VARIANT_LLVM_DEPLOYMENT.md](VARIANT_LLVM_DEPLOYMENT.md)。本次代码修改不代表服务器已部署，也不代表各版本真实构建已经通过。

健康采集、异常观察和本地保留策略见 [maintenance/README.md](../maintenance/README.md)。

## 容器内 CI Python

`container_python` 必须指向镜像中专门准备的 CI 虚拟环境（通常为 `/opt/venv/bin/python`），
不能配置成 `/usr/bin/python3` 或仅 `python3`。未显式配置时使用 Profile 的 `SEED_PYTHON`
或 `PYTHON_VENV_ACTIVATE` 对应解释器；它用于可信管理操作，并为 candidate/base 各自生成
可写的任务 venv。Codex 的构建、安装和测试使用相应任务 venv，环境修复不会污染预置环境。
任务 venv 直接复制预置包（包括 pip），不依赖系统 `ensurepip`；安装依赖使用 `"$PYTHON_BIN" -m pip`。
宿主机服务的 `python_bin` 与容器解释器独立，仍可使用宿主机 Python。
