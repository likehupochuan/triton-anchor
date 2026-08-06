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

_SUPPORTED_PTR_MODELS = {"structured", "axis_info", "hybrid", "gpu"}


def _validate_positive_int(owner: str, field_name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{owner}.{field_name} must be a positive integer")


def _validate_non_negative_int(owner: str, field_name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{owner}.{field_name} must be a non-negative integer")


def _validate_shape(
    owner: str, field_name: str, shape: tuple[int, ...], dimensions: int
) -> None:
    if not isinstance(shape, tuple) or len(shape) != dimensions:
        raise ValueError(
            f"{owner}.{field_name} must contain exactly {dimensions} dimensions"
        )
    for value in shape:
        _validate_positive_int(owner, field_name, value)


def _validate_supported_dtypes(owner: str, supported_dtypes: set[str]) -> None:
    if not supported_dtypes:
        raise ValueError(f"{owner}.supported_dtypes must not be empty")
    if any(
        not isinstance(dtype, str) or not dtype.strip() for dtype in supported_dtypes
    ):
        raise ValueError(f"{owner}.supported_dtypes must contain non-empty dtype names")


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
    tile_shape: tuple[int, int] = (8, 8)
    supported_dtypes: set[str] = field(default_factory=lambda: {"fp32", "fp16", "int8"})
    has_accumulator_tiles: bool = True
    vector_length: int = 256
    supports_pointwise: bool = True

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _validate_positive_int(owner, "num_matrix_registers", self.num_matrix_registers)
        _validate_shape(owner, "tile_shape", self.tile_shape, dimensions=2)
        _validate_positive_int(owner, "vector_length", self.vector_length)
        _validate_supported_dtypes(owner, self.supported_dtypes)


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

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _validate_positive_int(owner, "num_cores", self.num_cores)
        _validate_non_negative_int(owner, "local_mem_size", self.local_mem_size)
        _validate_non_negative_int(owner, "global_mem_size", self.global_mem_size)
        _validate_positive_int(owner, "dma_channels", self.dma_channels)
        _validate_positive_int(owner, "max_tensor_dims", self.max_tensor_dims)
        _validate_supported_dtypes(owner, self.supported_dtypes)


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

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _validate_positive_int(owner, "num_warps", self.num_warps)
        _validate_positive_int(owner, "warp_size", self.warp_size)
        _validate_non_negative_int(owner, "shared_mem_size", self.shared_mem_size)
        _validate_positive_int(owner, "num_stages", self.num_stages)
        _validate_positive_int(owner, "num_ctas", self.num_ctas)
        _validate_shape(owner, "cluster_dims", self.cluster_dims, dimensions=3)
        _validate_supported_dtypes(owner, self.supported_dtypes)


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
        """Validate identity, numeric limits, and paradigm-specific fields.

        Raises:
            ValueError: If any field is invalid or inconsistent with the
                selected compute paradigm.
        """
        for field_name in ("name", "arch_family"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"HWCapability.{field_name} must not be empty")

        if not isinstance(self.compute_paradigm, ComputeParadigm):
            raise TypeError(
                "HWCapability.compute_paradigm must be a ComputeParadigm value"
            )

        if (
            not isinstance(self.ptr_model, str)
            or self.ptr_model not in _SUPPORTED_PTR_MODELS
        ):
            supported = ", ".join(sorted(_SUPPORTED_PTR_MODELS))
            raise ValueError(
                f"Unsupported ptr_model '{self.ptr_model}'. "
                f"Expected one of: {supported}"
            )

        if self.preferred_adapter is not None and (
            not isinstance(self.preferred_adapter, str)
            or not self.preferred_adapter.strip()
        ):
            raise ValueError(
                "HWCapability.preferred_adapter must be a non-empty string or None"
            )

        _validate_positive_int("HWCapability", "num_cores", self.num_cores)

        cap_specs = {
            ComputeParadigm.AME_MATRIX: ("matrix_cap", MatrixCapability),
            ComputeParadigm.TENSOR_PROCESSOR: ("tensor_cap", TensorCapability),
            ComputeParadigm.GPGPU: ("gpgpu_cap", GPGPUCapability),
        }
        expected_name, expected_type = cap_specs[self.compute_paradigm]
        capabilities = {
            "matrix_cap": self.matrix_cap,
            "tensor_cap": self.tensor_cap,
            "gpgpu_cap": self.gpgpu_cap,
        }
        expected_capability = capabilities[expected_name]

        if expected_capability is None:
            raise ValueError(
                f"{self.compute_paradigm.name} paradigm requires {expected_name} "
                f"(hw: {self.name})"
            )
        if not isinstance(expected_capability, expected_type):
            raise TypeError(
                f"HWCapability.{expected_name} must be a {expected_type.__name__}"
            )

        unexpected = sorted(
            name
            for name, capability in capabilities.items()
            if name != expected_name and capability is not None
        )
        if unexpected:
            fields = ", ".join(unexpected)
            raise ValueError(
                f"{self.compute_paradigm.name} paradigm does not allow: {fields}"
            )

    def __post_init__(self) -> None:
        """Validate capability fields and resolve AnchorIRTrack.

        Design decision: compute_paradigm and anchor_ir_track are decoupled.
        Default mapping: AME/Tensor → LINALG, GPGPU → TRITON_GPU,
        but backends may override (e.g., a RISC-V GPU with Tensor Core).
        """
        if isinstance(self.compute_paradigm, str):
            try:
                object.__setattr__(
                    self,
                    "compute_paradigm",
                    ComputeParadigm(self.compute_paradigm),
                )
            except ValueError as exc:
                raise ValueError(
                    f"Unknown compute paradigm: {self.compute_paradigm}"
                ) from exc

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
