# 部署并验收 Triton LLVM profiles

本次操作要让服务器使用仓库中新增的 Triton 3.2、3.4、3.5、3.8 profile，
并验证 base / candidate 确实按各自冻结源码的 Triton major.minor 和完整 LLVM SHA
选择环境。按下面顺序操作；配置部署成功和真实构建通过分别记录。

四份 LLVM 的目录、权限、链接、可读性和内容摘要已经在服务器核验通过，
路径及摘要已写入 `config.example.json`。不需要重新安装 LLVM、修改权限或生成配置。
现有 3.0、3.1、3.3、3.6 profile 保持不变；只有 3.0 启用后端、PPL、FlagGems 和
backend-src。所有版本继续使用现有共享镜像。

## 1. 取得要部署的完整提交 SHA

**做什么：** 在开发仓库提交本次配置、文档和测试，推送至
`likehupochuan/triton-anchor` 的 `local-ci-unified`，按现有流程同步到 Gitee 控制镜像。
记录包含本次修改的完整 40 位提交 SHA，供下面的 `CONTROL_SHA` 使用。

**为什么：** 服务器从 Gitee 读取已提交的配置。本地未提交的修改不会随部署生效；
固定 SHA 可以确保代码、配置和后续验证对应同一个版本。

发布前在 Linux / WSL 的开发 checkout 根目录运行：

```bash
PYTHONPATH=scripts/local_ci python3 -m pytest scripts/local_ci/tests/test_prepare.py -q
git diff --check
```

**完成标志：** 测试和差异检查通过，目标 SHA 已从 Gitee 的 `local-ci-unified` 分支可达。
测试覆盖八个实际 profile 的选择、四个新增摘要及后端边界，不访问服务器依赖目录。
同一修改已验证且未再改动时，无需为部署重复运行这些开发测试。

## 2. 在服务器预览并应用控制更新

**做什么：** 以 `jiwang_ci` 登录，在现有控制目录执行。将下面的占位值替换为第一步的 SHA：

```bash
cd /home/jiwang_ci/local_ci/control_anchor
CONTROL_SHA='<已提交并同步至Gitee的40位SHA>'
CI_PYTHON=/home/jiwang_ci/local_ci/local-ci-control-venv/bin/python
CI_CONFIG=/home/jiwang_ci/local_ci/config/local-ci.json

"$CI_PYTHON" scripts/local_ci/prepare/control_update.py \
  --config "$CI_CONFIG" --expected-revision "$CONTROL_SHA"
```

检查输出中的 `revision` 是目标 SHA，`config_fields` 的差异符合此次更新。
确认后执行：

```bash
"$CI_PYTHON" scripts/local_ci/prepare/control_update.py \
  --config "$CI_CONFIG" --expected-revision "$CONTROL_SHA" --apply
```

**为什么：** 更新器检查 Gitee 可达性、快进关系和在途任务，把目标提交中的配置原子同步到
`local-ci.json`，并在需要时重启 Worker。手工复制配置或直接拉取 live checkout
不能完成这套协调。相同 SHA 下执行更新器也能修复运行配置偏差。

**完成标志：** 应用结果的 `revision` 为目标 SHA，`state` 为 `updated` 或 `current`。
如果返回 `deferred-active-task`，表示尚未完成更新；等现有任务及清理结束后重试同一命令。
如果报 checkout 有本地修改或非快进，先处理该问题，不强制覆盖运行目录。

运行配置仍由仓库唯一维护，不创建服务器覆盖 JSON，也不使用
`profiles/config.template.json` 替换它。后续命令继续在此登录会话和目录执行。

## 3. 为八个 profile 准备 runtime

**做什么：** 读取更新后的运行配置，调用现有环境管理器：

```bash
"$CI_PYTHON" - <<'PY'
import json
from pathlib import Path
import sys

config = json.loads(Path('/home/jiwang_ci/local_ci/config/local-ci.json').read_text())
sys.path.insert(0, str(Path(config['control_root']) / 'scripts/local_ci'))
from prepare.runtime import EnvironmentManager

manager = EnvironmentManager(config, config['state_dir'])
for profile_key, profile in config['profiles'].items():
    runtime = manager.ensure_image(profile_key, profile['llvm_hash'])
    print(profile_key, runtime['llvm_hash'], runtime['image_id'], runtime['state'])
PY
```

**为什么：** 控制更新只更新代码和配置，不为新增 profile 登记 ready runtime。
`ensure_image` 复用已验证且配置相同的环境，为新环境校验镜像、依赖内容和基础导入。
第四步的容器预检要求所有 profile 都有 ready 记录。

**完成标志：** 3.0、3.1、3.2、3.3、3.4、3.5、3.6、3.8 共八行均为 `ready`，
镜像 ID 相同，LLVM SHA 与配置一致。3.0 和 3.1 共用 LLVM，但保留各自 profile。
失败时按错误修复依赖或配置，再运行同一命令；不要修改正在被任务使用的依赖目录。

此步骤不构建或安装 Triton wheel，不能据此宣布版本验证通过。

## 4. 实测容器和运行配置

**做什么：** 使用现有凭据文件加载环境，然后运行正式预检：

```bash
"$CI_PYTHON" - <<'PY'
from pathlib import Path
import sys

sys.path.insert(0, str(Path('scripts/local_ci').resolve()))
from prepare.install import load_environment
from prepare.preflight import main

load_environment(Path('/home/jiwang_ci/local_ci/config/credentials.env'))
sys.argv = [
    'preflight.py',
    '--config', '/home/jiwang_ci/local_ci/config/local-ci.json',
    '--probe-runtime',
]
raise SystemExit(main())
PY
```

**为什么：** 已核验的宿主目录还需要在实际 Rootless Docker 容器中验证挂载、访问权限和资源限制；
正式预检同时检查运行账户、服务和凭据配置。上面的加载器复用安装器逻辑，不打印凭据值。
部署验收不使用 `--skip-notifications`，以免漏检正式运行所需的凭据。

**完成标志：** 命令退出码为 0，输出 `ready: true` 且 `runtime_probe.status: pass`。
若失败，保留对应检查项和错误，修复后再继续。
若新版本构建需要共享镜像中尚未提供的包，应单独安排镜像变更，不能通过借用 3.0 后端依赖解决。

## 5. 发起真实验证任务

**做什么：** 从 GitHub 现有网关提交任务，继续沿用 GitHub → Gitee → 服务器的路径。
逐个验证 3.0–3.6 和 3.8 的冻结源码；完整验收使用 `full=true`，因为普通任务会按实际改动选测。

在仓库 Actions → **CI Gateway** → **Run workflow** 中填写：

| 字段 | 值 |
|---|---|
| workflow 分支 | `local-ci-unified` |
| `mode` | `run` |
| `worker_revision_sha` | 已部署的完整控制 SHA，必须等于此次工作流所在的分支 HEAD |
| `full` | `true` |
| `pr_number` | 验证 PR 的编号；验证分支时填 `0` |
| `source_branch` | 分支任务填写实际被测分支名；PR 任务由 PR 决定 |
| `requested_sha` | PR 的 head SHA，或分支任务的当前 HEAD SHA |
| `action` | 手动分支任务填 `manual`；PR 任务留空 |

如果控制分支 HEAD 已前进，先部署经审核的新控制 SHA，再用该 SHA 派发；
工作流会拒绝 `worker_revision_sha` 与自身提交不一致的请求。
外部 fork PR 继续经过已有审批。

**为什么：** 只有真实任务的 frontend build/install/smoke 才能证明各版本可以构建运行。
环境准备与容器预检不执行这些构建。3.0 还要执行已有后端 smoke/JIT；
其他版本不具备后端能力。

再选择冻结源码满足下表的任务检查两侧环境。PR 的 base 是目标提交，candidate 是实际 merge/tested 提交；
分支任务的 base 是 HEAD 的第一父提交，candidate 是 HEAD。
不要只凭 PR 标题或分支名判断版本，也不要修改任务 JSON 来指定另一份 LLVM。

| 验证组合 | 为什么要做 | 完成标志 |
|---|---|---|
| 3.0 → 3.1 | 证明相同 LLVM 不会导致后端能力混用 | LLVM 相同，profile 不同；仅 base 启用后端，venv、输出和缓存独立 |
| 3.0 → 3.2 / 3.4 / 3.5.1 / 3.8 | 证明 base 不会误用 candidate 的 LLVM | 两侧 context 中源码 SHA、LLVM、profile、环境变量和 fingerprint 对应各自源码；两侧最小构建有实际证据 |
| 同版本、同 LLVM | 确认原有任务仍可运行 | 复用只读依赖，可写构建目录相互隔离 |

普通任务允许按改动不执行 base，但跨版本验收若缺少 base 构建证据，该项仍未完成。
需要补跑时，在该任务会话中要求 Agent 用各自的 `base-context.json` /
`candidate-context.json` 调用 `frontend_build`、`frontend_install` 和 `frontend_smoke`；
具体工具入口见 [工具文档](../tools/README.md)。后端或性能环境不可比时记录
`not_comparable`，不要将环境差异归为代码性能回退。

**完成标志：** 每个版本有实际命令和结果，跨版本任务有两侧 context 与构建证据，
Gitee 结果及 GitHub `Local CI Summary` 已回传。仅 route、dispatch 或 receive 成功不算构建通过。
已有封存结果需要补传时沿用接收器恢复流程，不重新构建，见 [网关文档](../../ci/README.md)。

## 6. 记录验收结果

**做什么：** 记录实际控制 SHA、部署和预检结论、每个版本的任务 ID、构建结果及证据路径，
以及跨版本验收中的 base/candidate 源码 SHA、profile 和 LLVM SHA。引用
`config.example.json` 中已登记的依赖路径、摘要及已有来源记录，列出未完成项。

**为什么：** 后续排查需要区分“配置已部署”“runtime 可用”和“真实构建通过”，
并能定位到同一次任务的源码与环境，不能用其中一种结果代替另一种。
