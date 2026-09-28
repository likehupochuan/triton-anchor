add_triton_library(TritonTransformsExtend
  ExtractMoveBackward.cpp
  InferAxisInfoInterfaceImpl.cpp
  WrapFuncBodyWithSingleBlock.cpp

  DEPENDS
  TritonTransformsExtendIncGen

  LINK_LIBS PUBLIC
  LinalgExtDialect
  TritonDialectUtils
  TritonLinalgUtils
  TritonInterfaceExtend
  MLIRIR
)
