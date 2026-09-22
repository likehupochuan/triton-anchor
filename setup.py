"""
triton-anchor: 统一构建脚本
===========================
替代原 Triton 的 643 行巨型 setup.py，只做三件事：
1. 调用 CMake 编译 C++ 代码（libtriton.so + _C.so）
2. 将编译产物复制到正确的 Python 包目录
3. 同时安装 triton 和 triton_anchor 两个包
"""
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import warnings
from pathlib import Path

import nanobind

# Suppress annoying setuptools warnings about C++ header directories looking like Python packages
warnings.filterwarnings("ignore", message=".*is absent from the `packages` configuration.*")


from setuptools import Extension, find_packages, setup
from setuptools.command.build_ext import build_ext
from setuptools.command.build_py import build_py
from distutils.command.clean import clean


def get_base_dir():
    return os.path.abspath(os.path.dirname(__file__))


def get_cmake_dir():
    plat_name = sysconfig.get_platform()
    python_version = sysconfig.get_python_version()
    dir_name = f"cmake.triton-3.8.{plat_name}-{sys.implementation.name}-{python_version}"
    cmake_dir = Path(get_base_dir()) / "build" / dir_name
    cmake_dir.mkdir(parents=True, exist_ok=True)
    return cmake_dir


def get_build_type():
    return os.environ.get("TRITON_BUILD_TYPE", "TritonRelBuildWithAsserts")


def get_env_with_keys(keys):
    for key in keys:
        val = os.environ.get(key, "")
        if val:
            return val
    return ""


class CMakeClean(clean):
    def initialize_options(self):
        clean.initialize_options(self)
        self.build_temp = str(get_cmake_dir())


class CMakeBuildPy(build_py):
    _excluded_modules = {
        "triton._filecheck",
        "triton.tools.gsan",
        "triton.tools.ragged_tma",
    }

    def build_module(self, module, module_file, package):
        package_name = ".".join(package) if isinstance(package, (list, tuple)) else package
        qualified_name = ".".join(filter(None, [package_name, module]))
        if qualified_name in self._excluded_modules:
            return None
        return super().build_module(module, module_file, package)

    def run(self):
        self.run_command('build_ext')
        result = super().run()
        self._strip_hardware_python_bindings()
        return result

    def _strip_hardware_python_bindings(self):
        """Patch only the build copy; keep vendored upstream Python pristine."""
        path = Path(self.build_lib) / "triton" / "compiler" / "code_generator.py"
        source = path.read_text()
        replacements = (
            (
                "from .._C.libtriton import ir, gluon_ir\n",
                "from .._C.libtriton import ir\n",
            ),
            (
                """        if is_gluon:
            from triton.experimental.gluon.language._semantic import GluonSemantic
            self.builder = gluon_ir.GluonOpBuilder(context, options.arch)
            self.semantic = GluonSemantic(self.builder)
        else:
            from triton.language.semantic import TritonSemantic
            self.builder = ir.builder(context)
            self.semantic = TritonSemantic(self.builder)
""",
                """        if is_gluon:
            raise RuntimeError(
                "Gluon IR belongs to an out-of-tree GPU plugin and is not "
                "available in the hardware-independent triton-anchor core"
            )
        from triton.language.semantic import TritonSemantic
        self.builder = ir.builder(context)
        self.semantic = TritonSemantic(self.builder)
""",
            ),
            (
                """    from ..experimental.gluon import language as ttgl
""",
                "",
            ),
            (
                """        ttgl.static_assert: execute_static_assert,
        ttgl.static_print: static_executor(print),
""",
                "",
            ),
        )
        for old, new in replacements:
            if old in source:
                source = source.replace(old, new, 1)
                continue
            # build_py can reuse an existing build/lib tree.  Treat an
            # already-applied replacement as success so repeated Wheel builds
            # remain deterministic.
            if (new and new in source) or (not new):
                continue
            if old not in source:
                raise RuntimeError(
                    f"upstream code_generator.py changed; missing expected block: {old!r}"
                )
        path.write_text(source)


class CMakeExtension(Extension):
    def __init__(self, name, path, sourcedir=""):
        Extension.__init__(self, name, sources=[])
        self.sourcedir = os.path.abspath(sourcedir)
        self.path = path


class CMakeBuild(build_ext):

    def run(self):
        try:
            subprocess.check_output(["cmake", "--version"])
        except OSError:
            raise RuntimeError("CMake must be installed")
        for ext in self.extensions:
            self.build_extension(ext)

    def build_extension(self, ext):
        ninja_dir = shutil.which('ninja')
        if ninja_dir is None:
            raise RuntimeError("Ninja must be installed")
        # 使用 extdir 作为 CMake 的根安装目录
        extdir = os.path.abspath(os.path.dirname(self.get_ext_fullpath(ext.name)))
        cmake_dir = get_cmake_dir()

        # Python 头文件路径
        python_include_dir = sysconfig.get_path("platinclude") or sysconfig.get_path("include")

        # LLVM 路径探测
        llvm_syspath = get_env_with_keys(["LLVM_SYSPATH"])

        cmake_args = [
            "-G", "Ninja",
            "-DCMAKE_MAKE_PROGRAM=" + ninja_dir,
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            "-DCMAKE_LIBRARY_OUTPUT_DIRECTORY=" + extdir,
            "-DTRITON_BUILD_PYTHON_MODULE=ON",
            "-DTRITON_EXT_ENABLED=ON",
            "-DTRITON_VERSION=3.8.0",
            "-DPython3_EXECUTABLE:FILEPATH=" + sys.executable,
            "-DPython3_INCLUDE_DIR=" + python_include_dir,
            "-Dnanobind_DIR=" + nanobind.cmake_dir(),
        ]

        # LLVM/MLIR 路径
        if llvm_syspath:
            cmake_args += [
                "-DLLVM_LIBRARY_DIR=" + os.path.join(llvm_syspath, "lib"),
                "-DLLVM_INCLUDE_DIRS=" + os.path.join(llvm_syspath, "include"),
                "-DLLVM_DIR=" + os.path.join(llvm_syspath, "lib", "cmake", "llvm"),
                "-DMLIR_DIR=" + os.path.join(llvm_syspath, "lib", "cmake", "mlir"),
            ]

        # 构建类型
        cfg = get_build_type()
        build_args = ["--config", cfg]

        if platform.system() != "Windows":
            cmake_args += ["-DCMAKE_BUILD_TYPE=" + cfg]
            max_jobs = os.getenv("MAX_JOBS", str(2 * os.cpu_count()))
            build_args += ['-j' + max_jobs]

        env = os.environ.copy()
        subprocess.check_call(
            ["cmake", get_base_dir()] + cmake_args,
            cwd=cmake_dir, env=env,
        )
        subprocess.check_call(
            ["cmake", "--build", "."] + build_args,
            cwd=cmake_dir,
        )

        # 收集上游 Triton 头文件到 triton/python/triton/include 目录
        triton_include_out_dir = os.path.join(get_base_dir(), "triton", "python", "triton", "include")
        shutil.rmtree(triton_include_out_dir, ignore_errors=True)
        os.makedirs(triton_include_out_dir, exist_ok=True)
        
        # 收集 triton-anchor 扩展头文件到 python/triton_anchor/include 目录
        anchor_include_out_dir = os.path.join(get_base_dir(), "python", "triton_anchor", "include")
        shutil.rmtree(anchor_include_out_dir, ignore_errors=True)
        os.makedirs(anchor_include_out_dir, exist_ok=True)

        def copy_headers(src_dir, out_dir, excluded_dirs=(), excluded_files=()):
            if not os.path.exists(src_dir): return
            for root, dirs, files in os.walk(src_dir):
                dirs[:] = [name for name in dirs if name not in excluded_dirs]
                for f in files:
                    if f in excluded_files:
                        continue
                    if f.endswith((".h", ".hpp", ".inc", ".def", ".td")):
                        src_path = os.path.join(root, f)
                        rel_path = os.path.relpath(src_path, src_dir)
                        dst_path = os.path.join(out_dir, rel_path)
                        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                        shutil.copy2(src_path, dst_path)

        # 拷贝 Triton 头文件
        copy_headers(
            os.path.join(get_base_dir(), "triton", "include"),
            triton_include_out_dir,
            excluded_dirs={
                "Analysis",
                "Conversion",
                "Gluon",
                "Target",
                "TritonGPU",
                "TritonInstrument",
                "TritonNvidiaGPU",
            },
            excluded_files={
                "GenericSwizzling.h",
                "LayoutUtils.h",
                "LinearLayout.h",
            },
        )
        copy_headers(os.path.join(cmake_dir, "triton", "include"), triton_include_out_dir)

        # 拷贝 Triton-Anchor 扩展头文件
        copy_headers(
            os.path.join(get_base_dir(), "csrc", "include"),
            anchor_include_out_dir,
        )
        copy_headers(os.path.join(cmake_dir, "csrc", "include"), anchor_include_out_dir)



def get_packages():
    """同时安装 triton 和 triton_anchor 两个 Python 包"""
    return (
        find_packages(
            where="triton/python",
            include=["triton", "triton.*"],
            exclude=[
                "triton.experimental.gluon*",
                "triton.experimental.gsan*",
                "triton.tools.triton_to_gluon_translator*",
            ],
        )
        + find_packages(where="python", include=["triton_anchor", "triton_anchor.*"])
    )


setup(
    name="triton-anchor",
    version="0.3.0",
    author="Triton Anchor Contributors",
    description="Unified Triton Compilation Frontend for custom AI accelerators",
    long_description="",
    license="Apache-2.0",
    package_dir={
        "": "python",
        "triton": "triton/python/triton",
    },
    packages=get_packages(),
    install_requires=["importlib-metadata; python_version < '3.10'"],
    package_data={
        "triton": [
            "include/**/*.h",
            "include/**/*.hpp",
            "include/**/*.inc",
            "include/**/*.def",
            "include/**/*.td",
        ],
        "triton_anchor": ["include/**/*.h", "include/**/*.hpp", "include/**/*.inc", "include/**/*.def", "include/**/*.td"],
    },
    include_package_data=True,
    ext_modules=[CMakeExtension("triton", "triton/python/triton/_C/")],
    cmdclass={
        "build_ext": CMakeBuild,
        "build_py": CMakeBuildPy,
        "clean": CMakeClean,
    },
    zip_safe=False,
    entry_points={
        "triton.adapters": [
            "triton-linalg = triton_anchor.adapters.triton_linalg_adapter:TritonLinalgAdapter",
        ]
    },
    keywords=["Compiler", "Deep Learning", "Triton"],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Compilers",
        "Programming Language :: Python :: 3",
    ],
    python_requires=">=3.10",
)
