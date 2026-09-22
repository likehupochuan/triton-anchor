# triton-anchor 构建与环境配置指南

本文档介绍了如何从零开始配置开发环境，并构建 `triton-anchor` 及其底层的相关依赖。由于 Triton 编译栈涉及大量的 C++ 和 LLVM 依赖，强烈建议在 Docker 环境中进行构建。

## 1. 基础容器与系统依赖 (Docker & APT)

推荐使用 Ubuntu 24.04 作为基础系统镜像，并在启动容器时挂载你的代码目录：

```bash
# 后台启动基础镜像（可根据需要挂载硬件特定的驱动目录，例如 /opt/tpuv7）
docker run --privileged -itd --name triton-anchor-dev \
    -v $PWD/workspace:/workspace \
    ubuntu:24.04

# 进入容器
docker exec -it triton-anchor-dev bash

# 更新包索引
apt update

# 安装基础编译工具链和依赖
apt install -y \
    build-essential \
    cmake \
    ninja-build \
    git \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    libz-dev \
    libzstd-dev \
    libxml2-dev
```

## 2. Python 虚拟环境与包管理器

为避免污染系统环境，推荐使用 [uv](https://github.com/astral-sh/uv) 管理 Python 虚拟环境，它比标准的 `pip` 快很多，且能够有效处理复杂的依赖树。

```bash
# 安装 uv
pip3 install uv --break-system-packages -i https://pypi.tuna.tsinghua.edu.cn/simple

# 创建并激活虚拟环境
uv venv /opt/venv
source /opt/venv/bin/activate

# 安装构建依赖
uv pip install setuptools wheel 'cmake>=3.20,<4.0' 'ninja>=1.11.1' nanobind==2.10.2
```

## 3. 准备 LLVM 工具链

因为 `triton-anchor` 需要与 LLVM/MLIR 进行 C++ 级别的链接，所以你需要提前准备好 LLVM 工具链。

### 选项 A：使用预编译的 LLVM Release 包

> **TODO**: 预编译包的获取途径与配置指南待完善。

### 选项 B：从源码手动编译 LLVM

1. 查找 Triton 当前依赖的 LLVM 版本。检查 `triton/cmake/llvm-info.json` 的
   `llvm_hash` 字段。当前主线 vendored Triton 使用该 JSON 同时记录预编译
   LLVM 的 commit、构建号和各平台校验和。

   当前 main 需要基于 Triton LLVM fork 的 `941a04e69ee8fe4c7a162b2f1e215aa8df867534` 提交进行构建。

2. 使用 `git checkout` 切换到对应的 LLVM 提交记录。如有需要，你可以对 LLVM 进行额外的源码修改。

3. [编译 LLVM](https://llvm.org/docs/CMake.html)。例如，你可以运行如下命令：

       $ cd $HOME/llvm-project  # 你的 LLVM 源码目录
       $ mkdir build
       $ cd build
       $ cmake -G Ninja -DCMAKE_BUILD_TYPE=Release -DLLVM_ENABLE_ASSERTIONS=ON ../llvm -DLLVM_ENABLE_PROJECTS="mlir;llvm;lld;clang" -DLLVM_TARGETS_TO_BUILD="host;<修改为你的目标架构，例如 RISCV>"
       $ ninja

4. 喝杯咖啡，这需要花费相当长的一段时间。

5. 编译完成后，你的 LLVM 构建目录为 `$HOME/llvm-project/build`。你可以直接将其作为参数传给 `envsetup.sh` 脚本来进行环境变量初始化。

## 4. 环境变量配置

在进行构建之前，你需要通过 `envsetup.sh` 脚本配置相关的环境变量。

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `WORKSPACE` | `/workspace` | 工作目录，支持手动指定 |
| `LLVM_BUILD_DIR` | `$WORKSPACE/llvm-release` | LLVM 编译产物主目录，支持手动指定 |

你可以通过如下方式来初始化环境：

```bash
# 使用默认路径 (/workspace/llvm-release)
source envsetup.sh

# 如果您有自定义的 LLVM 构建路径，请通过环境变量传入：
export LLVM_BUILD_DIR=/path/to/llvm-release
source envsetup.sh
```

## 5. 安装 triton-anchor

`triton-anchor` 包含了上游 Triton 的核心 C++ 基础设施以及自身的扩展管线（如 `triton-linalg`），负责构建完整的编译管线和分发 AnchorIR 规范。通过 `uv` 可以在隔离模式下进行编译与极速安装：

```bash
# 假设你已经克隆了代码到 /workspace/triton-anchor
cd /workspace/triton-anchor

# 1. 构建分发包 (wheel)
uv build --wheel --no-build-isolation

# 2. 安装生成的 wheel 包
uv pip install dist/triton_anchor-*.whl
```

## 6. 构建与集成硬件后端 (Out-of-Tree)

> **TODO**: 硬件后端的具体构建与集成流程待完善。
> 请参考对应的硬件后端仓库（如 `triton-all-backends/sophgo`）中的专用说明文档。

## 7. 验证安装

完成构建后，Triton 会通过 `entry_points` 自动发现已安装的后端。

```bash
# 验证 Python 包是否安装
python3 -c "import triton_anchor; print('triton-anchor loaded')"

# 验证底层硬件后端是否被 Triton 自动发现
python3 -c "from triton.backends import backends; print(backends)"
```

运行冒烟测试，全面验证 C++ 绑定、MLIR Dialect 注册、HWCapability、AnchorIR Validator、TTIR Pipeline 以及 TTIR 生成等核心功能：

```bash
python3 tests/test_smoke.py
```
