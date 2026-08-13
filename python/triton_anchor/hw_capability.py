"""
Hardware Capability & Compute Paradigm
=======================================

Core invariant of the unified frontend. HWCapability replaces the minimal
``GPUTarget(backend, arch, warp_size)`` with a rich, declarative description
of the target hardware.

Three compute paradigms are defined:
  - AME_MATRIX:  CPU-integrated matrix registers + matrix/vector ops
  - TENSOR_PROCESSOR:  Dedicated tensor compute units with own memory
  - GPGPU:  SIMT threads + shared memory + warp execution

Design decisions:
  - ``to_gpu_target()`` provides backward compatibility with existing
    ``GPUTarget``-based compile paths (triton_race, fantasy-triton).
  - Fields are append-only (never removed) to guarantee plugin stability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional, Set, Tuple, Literal, Union

if TYPE_CHECKING:
    from .anchor_ir import AnchorIRTrack


CapabilityDescriptor = Union[
    "MatrixCapability",
    "TensorCapability",
    "GPGPUCapability",
]

_DTYPE_ALIASES = {
    "float32": "fp32",
    "float16": "fp16",
    "bfloat16": "bf16",
    "int8": "int8",
    "i8": "int8",
    "fp32": "fp32",
    "fp16": "fp16",
    "bf16": "bf16",
}


# ═══════════════════════════════════════════════════════════════════════
# Compute Paradigm — the three fundamental ISA families
# ═══════════════════════════════════════════════════════════════════════


class ComputeParadigm(Enum):
    """Compute paradigm of the target hardware.

    This enum captures the *essential nature* of the hardware, not just
    a parameter — it determines the entire lowering strategy.
    """

    AME_MATRIX = "ame_matrix"
    """CPU-internal matrix extension (RISC-V AME, ARM SME).
    Characteristics: matrix registers, CPU cache hierarchy, no DMA."""

    TENSOR_PROCESSOR = "tensor"
    """Dedicated tensor processing unit (Sophgo TPU, Google TPU).
    Characteristics: independent memory space (HBM/SRAM), DMA-based data movement."""

    GPGPU = "gpgpu"
    """General-purpose GPU (NVIDIA, AMD, USC).
    Characteristics: SIMT threads, shared memory, warp execution."""


# ═══════════════════════════════════════════════════════════════════════
# Paradigm-Specific Capability Descriptors
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class MatrixCapability:
    """Capability descriptor for AME (Advanced Matrix Extension) hardware.

    Used by: SpacemiT X60, 玄铁 AME, ARM SME.
    """

    num_matrix_registers: int = 8
    tile_shape: Tuple[int, int] = (8, 8)
    supported_dtypes: Set[str] = field(default_factory=lambda: {"fp32", "fp16", "int8"})
    has_accumulator_tiles: bool = True
    vector_length: int = 256
    supports_pointwise: bool = True


@dataclass(frozen=True)
class TensorCapability:
    """Capability descriptor for dedicated tensor processor hardware.

    Used by: Sophgo BM1684X, Google TPU.
    """

    num_cores: int = 1
    local_mem_size: int = 0  # bytes, per-core local SRAM
    global_mem_size: int = 0  # bytes, HBM/DDR
    dma_channels: int = 1
    supported_dtypes: Set[str] = field(default_factory=lambda: {"fp32", "fp16", "int8"})
    max_tensor_dims: int = 4


@dataclass(frozen=True)
class GPGPUCapability:
    """Capability descriptor for gpGPU hardware.

    Used by: NVIDIA GPU, AMD GPU, USC GPU.
    """

    num_warps: int = 4
    warp_size: int = 32
    shared_mem_size: int = 49152  # bytes
    num_stages: int = 2
    num_ctas: int = 1
    cluster_dims: Tuple[int, int, int] = (1, 1, 1)
    supported_dtypes: Set[str] = field(
        default_factory=lambda: {"fp32", "fp16", "bf16", "int8"}
    )


# ═══════════════════════════════════════════════════════════════════════
# HWCapability — the unified hardware descriptor
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class HWCapability:
    """Declarative hardware capability descriptor.

    This is the **core invariant** of the unified frontend.  Backend plugins
    declare their hardware's capabilities through this dataclass, and the
    frontend uses it to drive compilation decisions:

    - ``compute_paradigm`` selects the lowering path (linalg vs triton_gpu)
    - ``ptr_model`` selects the pointer analysis adapter
    - ``preferred_adapter`` overrides automatic adapter selection
    - paradigm-specific caps (``matrix_cap``, ``tensor_cap``, ``gpgpu_cap``)
      provide fine-grained hardware parameters

    Stability guarantee: fields are append-only, never removed.

    Example::

        hw = HWCapability(
            name="sophgo-bm1684x",
            arch_family="tpu",
            compute_paradigm=ComputeParadigm.TENSOR_PROCESSOR,
            lowering_path="linalg",
            ptr_model="axis_info",
            tensor_cap=TensorCapability(num_cores=8, local_mem_size=16*1024*1024),
        )

    """

    # ── Identity ─────────────────────────────────────────────────────
    name: str  # e.g. "spacemit-x60", "sophgo-bm1684x"
    arch_family: str  # "riscv", "tpu", "gpu"

    # ── Compilation Strategy ─────────────────────────────────────────
    compute_paradigm: ComputeParadigm
    anchor_ir_track: "AnchorIRTrack"  # Decoupled from paradigm; backend controls
    ptr_model: Literal["structured", "axis_info", "hybrid", "gpu"]

    # ── Adapter Override ─────────────────────────────────────────────
    preferred_adapter: Optional[str] = None  # e.g. "triton-shared", "triton-linalg"

    # ── Paradigm-Specific Capabilities (mutually exclusive) ──────────
    matrix_cap: Optional[MatrixCapability] = None  # AME
    tensor_cap: Optional[TensorCapability] = None  # Tensor
    gpgpu_cap: Optional[GPGPUCapability] = None  # gpGPU

    # ── Optional Flags ───────────────────────────────────────────────
    enable_loop_unroll: bool = False
    num_cores: int = 1

    # ── Compatibility ────────────────────────────────────────────────

    def to_gpu_target(self):
        """Convert to a ``GPUTarget`` for backward compatibility.

        This allows HWCapability to be used in existing triton compilation
        paths that expect ``GPUTarget(backend, arch, warp_size)``.

        Returns:
            A ``GPUTarget``-compatible object.  If ``triton`` is not
            installed, returns a plain ``dict`` with the same fields.
        """
        backend = self._infer_backend_name()
        arch = self._infer_arch()
        warp_size = self._infer_warp_size()

        try:
            from triton.backends.compiler import GPUTarget

            return GPUTarget(backend=backend, arch=arch, warp_size=warp_size)
        except ImportError:
            # Fallback when triton is not installed (e.g., in tests)
            return {"backend": backend, "arch": arch, "warp_size": warp_size}

    @property
    def active_capability(self) -> CapabilityDescriptor:
        """Return the paradigm-specific capability descriptor in use.

        This gives callers a uniform way to inspect hardware limits without
        branching over ``matrix_cap``, ``tensor_cap``, and ``gpgpu_cap``.
        """
        if (
            self.compute_paradigm == ComputeParadigm.AME_MATRIX
            and self.matrix_cap is not None
        ):
            return self.matrix_cap
        if (
            self.compute_paradigm == ComputeParadigm.TENSOR_PROCESSOR
            and self.tensor_cap is not None
        ):
            return self.tensor_cap
        if self.compute_paradigm == ComputeParadigm.GPGPU and self.gpgpu_cap is not None:
            return self.gpgpu_cap
        raise ValueError(f"No active capability descriptor for hw: {self.name}")

    @property
    def supported_dtypes(self) -> Set[str]:
        """Return a copy of dtypes supported by the active capability descriptor."""
        return set(self.active_capability.supported_dtypes)

    def supports_dtype(self, dtype: str) -> bool:
        """Return True if the active capability supports ``dtype``.

        Common spelling aliases such as ``float32`` and ``bfloat16`` are
        normalized to the canonical ``fp32`` and ``bf16`` names used by
        capability descriptors.
        """
        normalized = _DTYPE_ALIASES.get(dtype.strip().lower(), dtype.strip().lower())
        return normalized in self.supported_dtypes

    def _infer_backend_name(self) -> str:
        """Infer the backend name string for GPUTarget compatibility."""
        # Map known hardware families to backend names
        _family_to_backend = {
            "tpu": "sophgo",
            "riscv": "spacemit",
            "gpu": "usc",
        }
        return _family_to_backend.get(self.arch_family, self.name.split("-")[0])

    def _infer_arch(self):
        """Infer architecture identifier for GPUTarget compatibility."""
        if self.gpgpu_cap:
            return 0  # Placeholder; real backends override
        return 0

    def _infer_warp_size(self) -> int:
        """Infer warp size for GPUTarget compatibility."""
        if self.gpgpu_cap:
            return self.gpgpu_cap.warp_size
        # Non-GPU paradigms don't have warps; use 0 as sentinel
        return 0

    # ── Validation ───────────────────────────────────────────────────

    def validate(self) -> None:
        """Validate that capability fields are self-consistent.

        Raises:
            ValueError: If paradigm-specific cap doesn't match compute_paradigm,
                or if lowering_path is inconsistent.
        """
        if self.compute_paradigm == ComputeParadigm.AME_MATRIX:
            if self.matrix_cap is None:
                raise ValueError(
                    f"AME_MATRIX paradigm requires matrix_cap (hw: {self.name})"
                )

        elif self.compute_paradigm == ComputeParadigm.TENSOR_PROCESSOR:
            if self.tensor_cap is None:
                raise ValueError(
                    f"TENSOR_PROCESSOR paradigm requires tensor_cap (hw: {self.name})"
                )

        elif self.compute_paradigm == ComputeParadigm.GPGPU:
            if self.gpgpu_cap is None:
                raise ValueError(f"GPGPU paradigm requires gpgpu_cap (hw: {self.name})")

    def __post_init__(self):
        """Validate capability fields and resolve AnchorIRTrack.

        Design decision: compute_paradigm and anchor_ir_track are decoupled.
        Default mapping: AME/Tensor → LINALG, GPGPU → TRITON_GPU,
        but backends may override (e.g., a RISC-V GPU with Tensor Core).
        """
        # Resolve string → enum if needed (backward compat)
        if isinstance(self.anchor_ir_track, str):
            from .anchor_ir import AnchorIRTrack

            object.__setattr__(
                self, "anchor_ir_track", AnchorIRTrack(self.anchor_ir_track)
            )

        self.validate()

    @property
    def lowering_path(self) -> str:
        """Backward-compatible lowering_path string.

        Returns:
            'linalg' or 'triton_gpu' based on anchor_ir_track.
        """
        return self.anchor_ir_track.value
