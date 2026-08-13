# 自定义硬件后端指南

`triton-anchor` 采用"前端核心 + 后端插件"架构。后端以独立的 Python 包形式存在，通过 `entry_points` 机制被 Triton 自动发现和加载，无需修改 Triton 或 triton-anchor 源码。

## 1. 架构总览

```
@triton.jit 装饰的函数
    │
    ▼  首次调用时触发编译
compiler.py — MyDeviceBackend(BaseBackend)
    │  add_stages() 定义编译管线
    │  TTIR → Linalg → 硬件 IR → .so/.elf
    │
    ▼  编译完成后加载执行
driver.py — MyDeviceDriver(DriverBase)
    │  launcher_cls → MyDeviceLauncher
    │  设备管理接口 (target, stream, device)
    │
    ▼  每次 kernel 调用时
MyDeviceLauncher.__call__()
    └── 加载 .so 并调用硬件运行时 API 执行
```

### 项目结构

```text
triton-mydevice-backend/
├── pyproject.toml             # 依赖与 entry_points 注册
├── CMakeLists.txt             # C++ 构建（如有自定义 Dialect/Pass）
├── csrc/                      # C++ 源码（MLIR Dialect、Conversion Pass、Pybind11）
├── triton_mydevice/           # Python 包（后端核心只需三个文件）
│   ├── __init__.py            # 导出 compiler_cls / driver_cls + 后端注册
│   ├── compiler.py            # 继承 BaseBackend，定义编译管线
│   └── driver.py              # 继承 DriverBase，定义 Launcher + Driver
└── tests/
    └── test_smoke.py
```

## 2. 插件注册

通过 `pyproject.toml` 注册到 `triton.backends` 分组：

```toml
[project.entry-points."triton.backends"]
my_device = "triton_mydevice"
```

## 3. 导出核心类

```python
# triton_mydevice/__init__.py
from .compiler import MyDeviceBackend
from .driver import MyDeviceDriver

compiler_cls = MyDeviceBackend   # Triton 的 _discover_backends() 读取
driver_cls = MyDeviceDriver
```

如果需要 `import triton_mydevice` 即激活后端，可在 `__init__.py` 中主动注入：

```python
from triton.backends import backends, Backend
from triton.runtime.driver import driver as _driver_config

backends["my_device"] = Backend(compiler=MyDeviceBackend, driver=MyDeviceDriver)
_driver_config.set_active(MyDeviceDriver())
```

## 4. 实现编译器后端 (`compiler.py`)

继承 `BaseBackend`，核心是 `add_stages()` 定义编译管线：

```python
# triton_mydevice/compiler.py
from triton.backends.compiler import BaseBackend, GPUTarget

class MyDeviceBackend(BaseBackend):
    binary_ext = 'so'

    @staticmethod
    def supports_target(target: GPUTarget):
        return target.backend == 'my_device'

    def parse_options(self, opts):
        # 解析 @triton.jit 的 kwargs，返回 Options 对象
        ...

    def pack_metadata(self, metadata):
        # 将编译元数据打包为 tuple，传递给 Launcher
        ...

    def load_dialects(self, ctx):
        # 加载自定义 MLIR 方言（如有 C++ Dialect）
        ...

    def add_stages(self, stages, options):
        stages["ttir"]   = lambda src, metadata: _make_ttir(src, metadata, options)
        stages["linalg"] = lambda src, metadata: _make_linalg(src, metadata, options)
        stages["so"]     = lambda src, metadata: _make_binary(src, metadata)
```

### 声明硬件能力与 dtype gate

后端可以用 `HWCapability` 声明计算范式、AnchorIR Track 和能力描述符，并通过统一的 dtype 查询接口决定是否接受某个 kernel 配置：

```python
from triton_anchor import HWCapability, ComputeParadigm
from triton_anchor.anchor_ir import AnchorIRTrack
from triton_anchor.hw_capability import TensorCapability

HW = HWCapability(
    name="my-device-v1",
    arch_family="tpu",
    compute_paradigm=ComputeParadigm.TENSOR_PROCESSOR,
    anchor_ir_track=AnchorIRTrack.LINALG,
    ptr_model="axis_info",
    tensor_cap=TensorCapability(
        num_cores=8,
        local_mem_size=16 * 1024 * 1024,
        supported_dtypes={"fp32", "fp16", "int8"},
    ),
)

def _make_linalg(mod, metadata, options):
    required_dtypes = {"float32", "int8"}
    unsupported = HW.unsupported_dtypes(required_dtypes)
    if unsupported:
        raise ValueError(
            f"my-device-v1 does not support required dtypes: {sorted(unsupported)}"
        )
    metadata["hw_supported_dtypes"] = sorted(HW.supported_dtypes)
    # ... run TTIR to Linalg conversion passes
    return mod
```

`supports_dtype()`、`supports_all_dtypes()` 和 `unsupported_dtypes()` 会将常见拼写如 `float32`、`float16` 和 `bfloat16` 规范化为能力描述符使用的 `fp32`、`fp16` 和 `bf16`。`supported_dtypes` 返回的是拷贝，调用方修改它不会改变后端声明的能力。

### 编译阶段

每个 stage 签名为 `(module, metadata) → module`，最后一个 stage 必须返回 `bytes`。

```python
def _make_ttir(mod, metadata, options):
    """Stage 1: 标准 TTIR 优化 (inliner, combine, cse, licm 等)。"""
    from triton._C.libtriton import ir, passes
    pm = ir.pass_manager(mod.context)
    passes.common.add_inliner(pm)
    passes.ttir.add_combine(pm)
    passes.common.add_canonicalizer(pm)
    # ... 其他标准 passes
    pm.run(mod)
    return mod


def _make_linalg(mod, metadata, options):
    """Stage 2: TTIR → Linalg（使用 triton-anchor 的 adapter pass 管线）。"""
    from triton._C.libtriton.anchor import anchor_passes as passes
    pm = ir.pass_manager(mod.context)
    tl = passes.triton_to_linalg
    tl.add_triton_to_linalg(pm)
    # ... 其他 anchor passes
    pm.run(mod)
    return mod


def _make_binary(mod, metadata) -> bytes:
    """Stage 3: Linalg → 硬件二进制。

    1. Dump IR 到文件
    2. 调用硬件编译工具链（你的编译器）
    3. 读取产物 .so 为 bytes 返回
    """
    metadata.setdefault("shared", 0)
    function_name = metadata["name"]
    # ... dump mod 到 .mlir 文件
    # ... 调用硬件编译器
    # ... 读取 .so
    metadata["so_path"] = so_path          # Launcher 通过此字段获取路径
    with open(so_path, "rb") as f:
        return f.read()                    # 必须返回 bytes
```

> [!CAUTION]
> 最后一个 stage **必须返回 `bytes`**，不要返回文件路径字符串。Triton 的缓存系统会接管这串字节流并写入 `~/.triton/cache`。

## 5. 实现运行时驱动 (`driver.py`)

driver.py 包含四个组件：

| 组件 | 职责 |
|------|------|
| `MyDeviceUtils` | 设备属性查询、`load_binary()` 透传编译产物 |
| `MyDeviceLauncher` | 解析 JIT 参数，调用硬件 API 执行 kernel |
| `MyDeviceInterface` | 模拟 CUDA 的 Event/Stream 语义（autotuner 需要） |
| `MyDeviceDriver` | 设备管理入口，继承 `DriverBase` |

```python
# triton_mydevice/driver.py
from triton.backends.compiler import GPUTarget
from triton.backends.driver import DriverBase


class MyDeviceUtils:
    """设备属性 + load_binary (透传编译产物给 Launcher)。"""
    @staticmethod
    def load_binary(name, kernel_obj, shared, device):
        return (None, kernel_obj, None, None)


class MyDeviceLauncher:
    """内核启动器。

    __init__(src, metadata): 从 src 提取 constants/signature，
                             计算非常量参数的位置映射。
    __call__(*args):         前 9 个是 triton 框架参数 (grid, stream, hooks...),
                             第 10 个起是用户 kernel 参数。
    """
    def __init__(self, src, metadata):
        # 解析 src.constants, src.signature → 参数位置映射
        self.kernel_name = getattr(metadata, "name", "kernel")
        self.so_path = getattr(metadata, "so_path", "")

    def __call__(self, *args, **kwargs):
        gridX, gridY, gridZ = args[0], args[1], args[2]
        kernel_user_args = args[9:]
        # 调用你的硬件运行时 API
        # my_runtime.launch(self.so_path, self.kernel_name, kernel_user_args, grid=...)


class MyDeviceInterface:
    """模拟 CUDA device/stream/event（triton benchmarking 需要）。"""
    # 实现 current_device(), synchronize(), Event(), Stream() 等


class MyDeviceDriver(DriverBase):
    def __init__(self):
        super().__init__()
        self.utils = MyDeviceUtils()
        self.launcher_cls = MyDeviceLauncher

    @staticmethod
    def is_active():
        return True

    def get_current_target(self):
        return GPUTarget("my_device", 0, 0)

    def get_device_interface(self):
        return MyDeviceInterface

    # 还需实现: get_current_device(), get_current_stream(),
    #           get_device_capability(), set_current_device() 等
```

## 6. 测试与验证

```python
# tests/test_smoke.py
import triton_mydevice
from triton.backends.compiler import GPUTarget
from triton_mydevice.compiler import MyDeviceBackend
from triton_mydevice.driver import MyDeviceDriver

target = GPUTarget("my_device", 0, 32)
assert MyDeviceBackend.supports_target(target)

driver = MyDeviceDriver()
assert driver.is_active()
print(f"current_target: {driver.get_current_target()}")
```

**一切就绪后，你就可以通过 `@triton.jit` 让 Triton 前端经由 `triton-anchor` 平滑地调用你的自定义芯片后端了！**
