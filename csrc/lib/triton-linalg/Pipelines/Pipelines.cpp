//===- Pipelines.cpp --------------------------------------------*- C++ -*-===//
//
// Copyright (C) [2022-2025] by Cambricon.
//
//===----------------------------------------------------------------------===//
//
// This file declares all pass pipelines
//
//===----------------------------------------------------------------------===//
#include "triton-linalg/Pipelines/Pipelines.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/Pass/PassManager.h"
#include "mlir/Pass/PassRegistry.h"
#include "mlir/Transforms/Passes.h"
#include "triton-linalg/Conversion/Passes.h"
#include "triton-linalg/Dialect/Triton/Transforms/Passes.h"
#include "llvm/ADT/StringRef.h"
#include <functional>

#include "mlir/Pass/Pass.h"

using namespace mlir;
using namespace triton;

namespace {

void buildTritonToLinalgPipeline(mlir::OpPassManager &pm) {

  pm.addPass(mlir::triton::createWrapFuncBodyWithSingleBlockPass());
  pm.addPass(mlir::createInlinerPass({}, nullptr));
  pm.addPass(mlir::createCanonicalizerPass());

  // Triton 3.8 removed the tensor-pointer operations handled by the old
  // CanonicalizeTriton and PointerStrengthReduction passes.
  pm.addPass(mlir::createCanonicalizerPass());
  pm.addPass(mlir::triton::createTritonToLinalgPass());
  pm.addNestedPass<mlir::func::FuncOp>(
      mlir::triton::createExtractLikeMoveBackwardPass());
  pm.addPass(mlir::createCanonicalizerPass());
  pm.addPass(mlir::triton::createArithToLinalgPass());
  pm.addPass(mlir::triton::createMathToLinalgPass());
  pm.addPass(mlir::createCSEPass());
  pm.addPass(mlir::createLoopInvariantCodeMotionPass());
  pm.addPass(mlir::triton::createWrapFuncBodyWithSingleBlockPass());

  pm.addPass(mlir::createCSEPass());
  pm.addPass(mlir::createCanonicalizerPass());
}

} // namespace

void ::mlir::triton::registerTritonLinalgPipelines() {
  PassPipelineRegistration<> triton_to_linalg(
      "triton-to-linalg",
      "Runs the triton to linalg dialect transformation pipeline",
      [](OpPassManager &passManager) {
        buildTritonToLinalgPipeline(passManager);
      });
  return;
}
