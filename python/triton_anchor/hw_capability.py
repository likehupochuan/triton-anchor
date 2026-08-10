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
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .anchor_ir import AnchorIRTrack


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


_SUPPORTED_PTR_MODELS = {"structured", "axis_info", "hybrid", "gpu"}


def _require_string(
    value: object, field_name: str, *, allow_none: bool = False
) -> None:
    if value is None:
        if allow_none:
            return
        raise ValueError(f"{field_name} must not be None")
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _require_positive_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be greater than zero")


def _require_nonnegative_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be greater than or equal to zero")


def _require_shape(value: object, field_name: str, dimensions: int) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    if len(value) != dimensions:
        raise ValueError(f"{field_name} must have {dimensions} dimensions")
    for index, dimension in enumerate(value):
        _require_positive_int(dimension, f"{field_name}[{index}]")


def _require_nonempty_dtype_set(value: object, field_name: str) -> None:
    if not isinstance(value, set):
        raise TypeError(f"{field_name} must be a set")
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    for dtype in value:
        _require_string(dtype, f"{field_name} item")


# ═══════════════════════════════════════════════════════════════════════
# Paradigm-Specific Capability Descriptors
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class MatrixCapability:
    """Capability descriptor for AME (Advanced Matrix Extension) hardware.

    Used by: SpacemiT X60, 玄铁 AME, ARM SME.
    """

    num_matrix_registers: int = 8
    tile_shape: tuple[int, int] = (8, 8)
    supported_dtypes: set[str] = field(default_factory=lambda: {"fp32", "fp16", "int8"})
    has_accumulator_tiles: bool = True
    vector_length: int = 256
    supports_pointwise: bool = True

    def __post_init__(self):
        _require_positive_int(
            self.num_matrix_registers, "matrix_cap.num_matrix_registers"
        )
        _require_shape(self.tile_shape, "matrix_cap.tile_shape", 2)
        _require_nonempty_dtype_set(
            self.supported_dtypes, "matrix_cap.supported_dtypes"
        )
        _require_positive_int(self.vector_length, "matrix_cap.vector_length")


@dataclass(frozen=True)
class TensorCapability:
    """Capability descriptor for dedicated tensor processor hardware.

    Used by: Sophgo BM1684X, Google TPU.
    """

    num_cores: int = 1
    local_mem_size: int = 0  # bytes, per-core local SRAM
    global_mem_size: int = 0  # bytes, HBM/DDR
    dma_channels: int = 1
    supported_dtypes: set[str] = field(default_factory=lambda: {"fp32", "fp16", "int8"})
    max_tensor_dims: int = 4

    def __post_init__(self):
        _require_positive_int(self.num_cores, "tensor_cap.num_cores")
        _require_nonnegative_int(self.local_mem_size, "tensor_cap.local_mem_size")
        _require_nonnegative_int(self.global_mem_size, "tensor_cap.global_mem_size")
        _require_positive_int(self.dma_channels, "tensor_cap.dma_channels")
        _require_nonempty_dtype_set(
            self.supported_dtypes, "tensor_cap.supported_dtypes"
        )
        _require_positive_int(self.max_tensor_dims, "tensor_cap.max_tensor_dims")


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
    cluster_dims: tuple[int, int, int] = (1, 1, 1)
    supported_dtypes: set[str] = field(
        default_factory=lambda: {"fp32", "fp16", "bf16", "int8"}
    )

    def __post_init__(self):
        _require_positive_int(self.num_warps, "gpgpu_cap.num_warps")
        _require_positive_int(self.warp_size, "gpgpu_cap.warp_size")
        _require_nonnegative_int(self.shared_mem_size, "gpgpu_cap.shared_mem_size")
        _require_positive_int(self.num_stages, "gpgpu_cap.num_stages")
        _require_positive_int(self.num_ctas, "gpgpu_cap.num_ctas")
        _require_shape(self.cluster_dims, "gpgpu_cap.cluster_dims", 3)
        _require_nonempty_dtype_set(self.supported_dtypes, "gpgpu_cap.supported_dtypes")


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
    anchor_ir_track: AnchorIRTrack  # Decoupled from paradigm; backend controls
    ptr_model: Literal["structured", "axis_info", "hybrid", "gpu"]

    # ── Adapter Override ─────────────────────────────────────────────
    preferred_adapter: str | None = None  # e.g. "triton-shared", "triton-linalg"

    # ── Paradigm-Specific Capabilities (mutually exclusive) ──────────
    matrix_cap: MatrixCapability | None = None  # AME
    tensor_cap: TensorCapability | None = None  # Tensor
    gpgpu_cap: GPGPUCapability | None = None  # gpGPU

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
        _require_string(self.name, "name")
        _require_string(self.arch_family, "arch_family")
        _require_string(self.preferred_adapter, "preferred_adapter", allow_none=True)
        _require_positive_int(self.num_cores, "num_cores")

        if not isinstance(self.compute_paradigm, ComputeParadigm):
            raise TypeError("compute_paradigm must be a ComputeParadigm")

        if self.ptr_model not in _SUPPORTED_PTR_MODELS:
            supported = ", ".join(sorted(_SUPPORTED_PTR_MODELS))
            raise ValueError(f"ptr_model must be one of: {supported}")

        capability_specs = {
            ComputeParadigm.AME_MATRIX: ("matrix_cap", MatrixCapability),
            ComputeParadigm.TENSOR_PROCESSOR: ("tensor_cap", TensorCapability),
            ComputeParadigm.GPGPU: ("gpgpu_cap", GPGPUCapability),
        }
        required_field, required_type = capability_specs[self.compute_paradigm]
        capabilities = {
            "matrix_cap": self.matrix_cap,
            "tensor_cap": self.tensor_cap,
            "gpgpu_cap": self.gpgpu_cap,
        }

        for field_name, capability in capabilities.items():
            if field_name == required_field:
                if capability is None:
                    raise ValueError(
                        f"{self.compute_paradigm.name} paradigm requires "
                        f"{required_field} (hw: {self.name})"
                    )
                if not isinstance(capability, required_type):
                    raise TypeError(
                        f"{required_field} must be {required_type.__name__}"
                    )
                continue

            if capability is not None:
                raise ValueError(
                    f"{field_name} must be empty for {self.compute_paradigm.name} "
                    f"paradigm (hw: {self.name})"
                )

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
