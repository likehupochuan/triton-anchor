"""
Unified TTIR Pipeline
======================

Extracts the 8 mandatory TTIR normalization passes that are shared
across all three projects (spine-triton, triton_race, fantasy-triton).

This is a **core invariant** — the pass list is append-only and
synchronized with upstream Triton.

The pipeline also supports conditional passes controlled by ``HWCapability``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .hw_capability import HWCapability


def build_ttir_pipeline(pm, hw: Optional[HWCapability] = None):
    """Build the standard TTIR optimization pipeline.

    This is extracted from triton_race's ``_make_ttir()`` and is identical
    to the 8 mandatory passes used by triton-anchor 0.3.

    Args:
        pm: An ``mlir.PassManager`` instance.
        hw: Optional ``HWCapability``.  If provided, conditional passes
            are added based on hardware capabilities.

    Usage::

        from triton._C.libtriton import ir, passes

        mod = ...  # TTIR module
        pm = ir.pass_manager(mod.context)
        build_ttir_pipeline(pm, hw=my_hw_capability)
        pm.run(mod)

    Note:
        This function requires ``triton._C.libtriton`` to be available.
        It will raise ``ImportError`` if Triton is not installed.
    """
    from triton._C.libtriton import passes

    # ═══════════════════════════════════════════════════════════════════
    # Mandatory Passes — target-independent TTIR normalization.
    # ═══════════════════════════════════════════════════════════════════
    passes.common.add_inliner(pm)
    passes.ttir.add_rewrite_tensor_descriptor_to_pointer(pm)
    passes.ttir.add_combine(pm)
    passes.common.add_canonicalizer(pm)
    passes.ttir.add_reorder_broadcast(pm)
    passes.common.add_cse(pm)
    passes.common.add_licm(pm)
    passes.common.add_symbol_dce(pm)

    # ═══════════════════════════════════════════════════════════════════
    # Conditional Passes — controlled by HWCapability
    # ═══════════════════════════════════════════════════════════════════
    if hw is not None:
        # Optional loop unrolling (safe to skip if unavailable)
        if hw.enable_loop_unroll:
            _try_add_pass(passes.ttir, "add_loop_unroll", pm)

        # FlagTree extra optimization (optional, auto-probe)
        _try_add_pass(passes.ttir, "add_expression_restructing", pm)


def _try_add_pass(module, pass_name, pm, **kwargs):
    """Safely try to add a pass. Silently skip if not available.

    For optional passes (e.g., add_expression_restructing, add_loop_unroll)
    whose absence does not affect compilation correctness.
    """
    fn = getattr(module, pass_name, None)
    if fn is not None:
        fn(pm, **kwargs) if kwargs else fn(pm)
        return True
    return False


def make_ttir(mod, metadata: dict, hw: Optional[HWCapability] = None):
    """Convenience function: build pipeline + run it on a module.

    This mirrors the signature of triton_race's ``_make_ttir(mod, metadata, options)``.

    Args:
        mod: An MLIR module (``ir.Module``).
        metadata: Compilation metadata dict (mutated in-place).
        hw: Optional ``HWCapability``.

    Returns:
        The optimized MLIR module (same object, mutated in-place).
    """
    from triton._C.libtriton import ir

    pm = ir.pass_manager(mod.context)
    pm.enable_debug()
    build_ttir_pipeline(pm, hw=hw)
    pm.run(mod)
    return mod


def inject_hw_attributes(mod, hw: HWCapability, metadata: dict):
    """将硬件能力信息注入 MLIR module 属性和编译元数据中。

    在 TTIR 优化之后、硬件感知 IR 降级之前调用。
    后端插件可通过 ``on_ttir_ready()`` hook 注入额外属性。

    Args:
        mod: MLIR module。
        hw: 目标硬件能力描述。
        metadata: 编译元数据 dict（就地更新）。
    """
    try:
        from triton._C.libtriton import ir

        builder = ir.builder(mod.context)

        # 整型硬件属性注入到 MLIR module（供下游 C++ pass 使用）
        if hw.arch_family == "riscv" and hw.matrix_cap:
            mod.set_attr("hw.num_threads", builder.get_int32_attr(hw.num_cores))
        elif hw.arch_family == "tpu" and hw.tensor_cap:
            mod.set_attr("hw.core_num", builder.get_int32_attr(hw.tensor_cap.num_cores))
        elif hw.arch_family == "gpu" and hw.gpgpu_cap:
            mod.set_attr(
                "hw.num_warps",
                builder.get_int32_attr(hw.gpgpu_cap.num_warps),
            )

    except ImportError:
        pass

    # 硬件描述信息通过 metadata dict 传递给下游 Python 代码
    metadata["hw_name"] = hw.name
    metadata["hw_paradigm"] = hw.compute_paradigm.value
    metadata["hw_arch_family"] = hw.arch_family
