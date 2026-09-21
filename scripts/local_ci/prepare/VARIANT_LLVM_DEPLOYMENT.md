# Base / candidate LLVM 环境部署交接

本方案让同一任务按 base 与 candidate 各自源码中的 LLVM SHA 和 Triton major.minor
选择可信 profile。分支名不再决定环境，`branch_profiles` 已停用。
profile 的后端能力仍只对已验证的 Triton 3.0 开启。

这是服务器侧 Codex 的部署交接，不是已经部署完成的记录。编写本文时本次代码尚未提交、推送或部署；
操作时应取得实际已经审核、提交并同步到 Gitee 的完整控制 SHA。不要直接执行可变分支尖端。
沿用现有部署流程处理在途任务并保留任务证据；凭据位置和注入方式保持既有配置。

## 1. 已核对的版本与待完成配置

| 源码版本 | LLVM 完整 SHA | 来源 | 仓库部署配置 |
|---|---|---|---|
| 3.0.0 | `10dc3a8e916d73291269e5e2b82dd22681489aa1` | 现有兼容矩阵 | 已有，后端开启 |
| 3.1.0 | `10dc3a8e916d73291269e5e2b82dd22681489aa1` | [RACE #88](https://github.com/RACE-org/triton-anchor/pull/88) | 已补 profile，复用 3.0 的只读 LLVM，后端关闭 |
| 3.2.0 | `86b69c31642e98f8357df62c09d118ad1da4e16a` | [RACE #87](https://github.com/RACE-org/triton-anchor/pull/87) | 待真实安装路径与目录摘要 |
| 3.3.0 | `a66376b0dc3b2ea8a84fda26faca287980986f78` | 现有兼容矩阵 | 已有，后端关闭 |
| 3.4.0 | `8957e64a20fc7f4277565c6cfe3e555c119783ce` | [RACE #89](https://github.com/RACE-org/triton-anchor/pull/89) | 待真实安装路径与目录摘要 |
| 3.5.1 | `7d5de3033187c8a3bb4d2e322f5462cdaf49808f` | [RACE #90](https://github.com/RACE-org/triton-anchor/pull/90) | 待真实安装路径与目录摘要；profile 版本为 `3.5` |
| 3.6.0 | `a992f29451b9e140424f35ac5e20177db4afbdc0` | 现有兼容矩阵 | 已有，后端关闭 |
| 3.8.0 | `941a04e69ee8fe4c7a162b2f1e215aa8df867534` | [RACE #91](https://github.com/RACE-org/triton-anchor/pull/91) | 待真实安装路径与目录摘要 |

上述 SHA 是核对时源码的声明，不是运行时硬编码映射；以后任务仍读取自己的冻结源码。
3.8 的声明位于 `triton/cmake/llvm-info.json`，同目录 `llvm-build-info.json` 的 SHA
用途不同，不参与环境选择。现有共享解析器已支持前者并忽略后者。

3.0 与 3.1 使用同一 LLVM，不必重复安装，但后端能力不同，必须分别保留可信 profile。
服务器称已安装新增 LLVM；桌面侧没有核验其路径、目录摘要、来源和可运行性，
因此没有用猜测路径或空摘要向生产 `config.example.json` 添加剩余四个 profile。
`profiles/config.template.json` 的空值仅为其他部署参考，不得替换生产运行配置。

## 2. 只读盘点服务器

以 `jiwang_ci` 用户操作。先从现有运行配置中确认 `dependency_root`、顶层 `image` 和
已有 profiles；只读取这些非敏感字段，不读取或输出 `credentials.env`、模型配置或认证内容。

分别核对新增四份 LLVM：

- 安装目录的真实绝对路径位于现有 `dependency_root` 内，目录本身不是符号链接。
- 包来源或构建记录证明与表中完整 LLVM SHA 一致；目录名和 `llvm-config --version`
  都不能单独证明源码 SHA。
- `bin/llvm-config --version`、`--host-target`、`--shared-mode` 可执行，头文件和 CMake
  package 完整，工具链架构、libstdc++/glibc 等 ABI 与现有共享镜像兼容。
- 目录内容属于 CI 用户，容器映射用户可读/可遍历，组和其他用户不可写；内部链接相对、
  不越界且目标存在。详细规则见 [DEPENDENCY_MOUNTS.md](DEPENDENCY_MOUNTS.md)。

不要修改正在使用的依赖目录。若现有包需要修正权限、链接或内容，准备新的版本目录后再计算摘要。
实际目录的摘要必须使用 `prepare.artifacts.tree_digest`；不能拿压缩包 SHA256 替代。

## 3. 在独立源码 checkout 补齐四个 profile

将待部署控制提交检出到独立开发目录，不能在运行中的
`/home/jiwang_ci/local_ci/control_anchor` 修改文件。以下命令从该独立 checkout 根目录运行，
四个参数按 3.2、3.4、3.5、3.8 顺序替换为前一步已核验的真实安装目录。
脚本仅修改独立 checkout 的仓库配置，不修改服务器 `local-ci.json`。

```bash
python3 - /actual/llvm-3.2 /actual/llvm-3.4 /actual/llvm-3.5 /actual/llvm-3.8 <<'PY'
import copy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path('scripts/local_ci').resolve()))
from prepare.artifacts import tree_digest
from prepare.dependency_mounts import dependency_mounts, validate_mounted_llvm

config_path = Path('scripts/local_ci/prepare/config.example.json')
config = json.loads(config_path.read_text())
assert Path.cwd().resolve() != Path(config['control_root']).resolve(), 'Use a separate checkout'
revisions = {
    '3.2': '86b69c31642e98f8357df62c09d118ad1da4e16a',
    '3.4': '8957e64a20fc7f4277565c6cfe3e555c119783ce',
    '3.5': '7d5de3033187c8a3bb4d2e322f5462cdaf49808f',
    '3.8': '941a04e69ee8fe4c7a162b2f1e215aa8df867534',
}
assert len(sys.argv) == 5, 'Supply the four verified LLVM installation directories'
for (version, revision), source_arg in zip(revisions.items(), sys.argv[1:]):
    source = Path(source_arg).resolve(strict=True)
    profile = copy.deepcopy(config['profiles']['triton_v3.3'])
    profile.update(name='triton-' + version, triton_version=version,
                   llvm_hash=revision, backend_enabled=False)
    profile['llvm'] = {'mode': 'mount', 'commit': revision}
    profile['mounts'] = [{
        'source': str(source),
        'target': '/opt/local-ci/runtime/deps/llvm-' + revision,
        'read_only': True,
        'sha256': tree_digest(source),
    }]
    mounts = dependency_mounts(config, profile, verify_content=True)
    validate_mounted_llvm(profile, mounts)
    config['profiles']['triton_v' + version] = profile
    print(version, revision, source, profile['mounts'][0]['sha256'])
config.pop('branch_profiles', None)
config['profiles'] = dict(sorted(config['profiles'].items()))
config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
PY

git diff --check
git diff -- scripts/local_ci/prepare/config.example.json
```

这个补齐步骤复用现有前端 profile 的镜像和基础环境，不改变 3.0 的后端、PPL、FlagGems，
也不更新共享镜像。如果新的 Triton 构建另需 Python 包或系统库，先报告缺失项并安排独立的
共享镜像变更与验证；不能把主机装好 LLVM 当作容器中完整构建已经可用。

从独立 checkout 运行静态预检，校验真实依赖内容：

```bash
python3 scripts/local_ci/prepare/preflight.py \
  --config scripts/local_ci/prepare/config.example.json \
  --configuration-only --skip-notifications
```

`--skip-notifications` 仅用于配置开发，正式部署仍需已有私有凭据。处理预检失败后，
提交独立 checkout 中的 `config.example.json`，经正常代码流程推送并同步至 Gitee 控制镜像。
将代码与配置最终的完整提交 SHA 记录在交接结果中。不要另建本地覆盖 JSON。

## 4. 经现有控制更新流程部署

确认最终 SHA 已从 Gitee 控制分支可达，live checkout 干净、没有尚需原环境的在途任务。
若正常任务驱动更新已经抵达这个 SHA，就只核对配置及版本；需要人工部署时，
从服务器现有控制 checkout 使用实际 SHA 预览，再应用：

```bash
python3 scripts/local_ci/prepare/control_update.py \
  --config /home/jiwang_ci/local_ci/config/local-ci.json \
  --expected-revision <APPROVED_40_CHARACTER_SHA>

python3 scripts/local_ci/prepare/control_update.py \
  --config /home/jiwang_ci/local_ci/config/local-ci.json \
  --expected-revision <APPROVED_40_CHARACTER_SHA> --apply
```

该流程从目标提交读取仓库配置，结构化比较并原子同步运行副本，保持 CI 用户所有和 600 权限，
然后复用一次 Worker 重启加载代码及配置。相同 SHA 下也可用于修复配置偏差。
不得先手工把未提交配置复制到运行副本，也不得直接把 live checkout 改脏。

确认更新返回 `state: updated` 或 `current`；`deferred-active-task` 表示尚未部署，
应等已有任务退出后再应用。控制更新本身不为新 profile 创建已验证的 runtime 记录。
更新完成后，以同一 CI 用户运行以下步骤，准备所有 profile 的 runtime：

```bash
python3 - <<'PY'
import json
from pathlib import Path
import sys

config = json.loads(Path('/home/jiwang_ci/local_ci/config/local-ci.json').read_text())
sys.path.insert(0, str(Path(config['control_root']) / 'scripts/local_ci'))
from prepare.runtime import EnvironmentManager

manager = EnvironmentManager(config, config['state_dir'])
for profile_key, profile in sorted(config['profiles'].items()):
    runtime = manager.ensure_image(profile_key, profile['llvm_hash'])
    print(profile_key, runtime['llvm_hash'], runtime['image_id'], runtime['state'])
PY
```

`ensure_image` 复用已验证的相同环境，并为新增 profile 校验共享镜像、只读依赖和基础导入，
登记 ready runtime；它不构建或安装 frontend/backend wheel，也不代表真实构建已通过。
只有所有 profile 都准备完成后，才按已有预检入口运行
`preflight.py --config <运行配置> --probe-runtime`；该预检需要每个 profile 已有 ready runtime。
使用现有私有凭据注入方式，不在命令行或输出中展开 token。若挂载来源、目录内容或 ABI
有问题，先保留错误证据并修复可信配置/依赖，不绕过校验。

## 5. 服务器验收与反馈

1. 对 3.0–3.6 和 3.8 的冻结源码分别做 frontend build/install/smoke；配置和 runtime probe
   通过只证明基础条件，不代表这八个版本真实构建通过。3.0 后端另做已有 smoke/JIT，
   其他版本不得借用 3.0 后端能力。
2. 验证 3.0 → 3.1：共享同一只读 LLVM，两个 profile/capability 正确，venv、输出、缓存独立。
3. 验证 3.0 → 3.2/3.4/3.5.1/3.8 的跨 LLVM 任务。检查 base-context 与 candidate-context
   各自的源码 SHA、LLVM、profile、env、backend capability 和 fingerprint；分别执行两侧
   最小构建以证明没有误用 candidate LLVM。
4. base 可按改动不执行；但一旦执行必须走自己的 context。性能环境不可比时输出
   `not_comparable`，不能把版本/后端差异错误报告成代码性能回退。
5. 验证同环境任务仍正常运行，升级前已派发任务的兼容读取、已封存结果上传及 GitHub
   回传不丢失。不要重跑已有封存结果来验证上传。

反馈包含实际控制 SHA、完整非敏感配置差异、LLVM 来源及目录摘要、预检结论、每个版本
实际运行的命令与结果、跨版本任务 ID 和未完成项。不要将“已配置”写成“全部构建已通过”。
