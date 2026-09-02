if(TTGPU)
  set(TRITON_GPU_LLVM_TTGPU 
    "${CMAKE_SOURCE_DIR}/csrc/lib/ttgpu/Conversion/TritonGPUToLLVM/ConvertLayoutOpToLLVM/SharedToDotOperandFMA.cpp"
    "${CMAKE_SOURCE_DIR}/csrc/lib/ttgpu/Conversion/TritonGPUToLLVM/DotOpToLLVM/FMA.cpp"
    "${CMAKE_SOURCE_DIR}/csrc/lib/ttgpu/Conversion/TritonGPUToLLVM/TypeConverter.cpp"
    "${CMAKE_SOURCE_DIR}/csrc/lib/ttgpu/Conversion/TritonGPUToLLVM/Utility.cpp"
    "${CMAKE_SOURCE_DIR}/csrc/lib/ttgpu/Conversion/TritonGPUToLLVM/ElementwiseOpToLLVM.cpp"
    "${CMAKE_SOURCE_DIR}/csrc/lib/ttgpu/Conversion/TritonGPUToLLVM/PrintOpToLLVM.cpp"
  )
else()
  set(TRITON_GPU_LLVM_TTGPU 
    ConvertLayoutOpToLLVM/SharedToDotOperandFMA.cpp
    DotOpToLLVM/FMA.cpp
    TypeConverter.cpp
    Utility.cpp
    ElementwiseOpToLLVM.cpp
    PrintOpToLLVM.cpp
  )
endif()

add_triton_library(TritonGPUToLLVM
    # ConvertLayoutOpToLLVM/SharedToDotOperandFMA.cpp
    # DotOpToLLVM/FMA.cpp
    # TypeConverter.cpp
    # Utility.cpp
    # ElementwiseOpToLLVM.cpp
    ${TRITON_GPU_LLVM_TTGPU}
    MemoryOpToLLVM.cpp
    AssertOpToLLVM.cpp
    ViewOpToLLVM.cpp
    MakeRangeOpToLLVM.cpp
    HistogramOpToLLVM.cpp
    AllocateSharedMemory.cpp
    ReduceOpToLLVM.cpp
    ScanOpToLLVM.cpp
    ConvertLayoutOpToLLVM.cpp
    ControlFlowOpToLLVM.cpp
    FuncOpToLLVM.cpp
    SPMDOpToLLVM.cpp
    DecomposeUnsupportedConversions.cpp
    # PrintOpToLLVM.cpp

    DEPENDS
    TritonGPUConversionPassIncGen

    LINK_LIBS PUBLIC
    MLIRIR
    MLIRPass
    MLIRGPUDialect
    MLIRGPUToNVVMTransforms
    MLIRGPUToROCDLTransforms
    MLIRGPUTransforms
    TritonAnalysis
    TritonIR
    TritonGPUIR
    TritonGPUTransforms
    TritonNvidiaGPUTransforms
)
