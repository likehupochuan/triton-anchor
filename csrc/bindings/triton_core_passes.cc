#include "mlir/Conversion/Passes.h"
#include "mlir/Pass/PassManager.h"
#include "mlir/Transforms/Passes.h"
#include "passes.h"
#include "triton/Dialect/Triton/Transforms/Passes.h"
#include "triton/Tools/PluginUtils.h"
#include <memory>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>
#include <stdexcept>
#include <string>

namespace py = nanobind;

namespace {

void init_triton_passes_common(py::module_ &m) {
  using namespace mlir;
  ADD_PASS_WRAPPER_0("add_sccp", createSCCPPass);
  ADD_PASS_WRAPPER_0("add_symbol_dce", createSymbolDCEPass);
  ADD_PASS_WRAPPER_0("add_inliner", createInlinerPass);
  ADD_PASS_WRAPPER_0("add_canonicalizer", createCanonicalizerPass);
  ADD_PASS_WRAPPER_0("add_cse", createCSEPass);
  ADD_PASS_WRAPPER_0("add_licm", createLoopInvariantCodeMotionPass);
  ADD_PASS_WRAPPER_0("print_ir", createPrintIRPass);
}

void init_triton_passes_ttir(py::module_ &m) {
  using namespace mlir::triton;
  ADD_PASS_WRAPPER_0("add_combine", createTritonCombineOps);
  ADD_PASS_WRAPPER_0("add_reorder_broadcast", createTritonReorderBroadcast);
  ADD_PASS_WRAPPER_0("add_rewrite_tensor_descriptor_to_pointer",
                     createTritonRewriteTensorDescriptorToPointer);
  ADD_PASS_WRAPPER_0("add_loop_unroll", createTritonLoopUnroll);
  ADD_PASS_WRAPPER_0("add_triton_licm", createTritonLoopInvariantCodeMotion);
  ADD_PASS_WRAPPER_0("add_loop_aware_cse", createTritonLoopAwareCSE);
}

void init_triton_passes_convert(py::module_ &m) {
  using namespace mlir;
  ADD_PASS_WRAPPER_0("add_scf_to_cf", createSCFToControlFlowPass);
  ADD_PASS_WRAPPER_0("add_cf_to_llvmir", createConvertControlFlowToLLVMPass);
  ADD_PASS_WRAPPER_0("add_index_to_llvmir", createConvertIndexToLLVMPass);
  ADD_PASS_WRAPPER_0("add_arith_to_llvmir", createArithToLLVMConversionPass);
  ADD_PASS_WRAPPER_0("add_reconcile_unrealized_casts",
                     createReconcileUnrealizedCastsPass);
}

void init_plugin_passes(py::module_ &m) {
  auto mPtr = std::make_shared<py::module_>(m);
  m.def(
      "extend_with",
      [mPtr](const std::string &path) {
        auto pluginOrErr = mlir::triton::plugin::TritonPlugin::load(path);
        if (!pluginOrErr)
          throw std::runtime_error(
              llvm::toString(pluginOrErr.takeError()));
        auto plugin = std::move(*pluginOrErr);
        py::gil_scoped_acquire acquire;
        for (const auto &pass : plugin.listPasses()) {
          std::string wrapped = std::string("add_") + pass.name;
          mPtr->def(
              wrapped.c_str(),
              [pass](mlir::PassManager &pm, std::vector<std::string> args) {
                pass.addPass(&pm, args);
              },
              py::arg("pm"), py::arg("args") = std::vector<std::string>());
        }
      },
      "Load pass registrations from an out-of-tree Triton extension.");
}

} // namespace

void init_triton_passes(py::module_ &m) {
  auto common = m.def_submodule("common");
  init_triton_passes_common(common);
  auto convert = m.def_submodule("convert");
  init_triton_passes_convert(convert);
  auto ttir = m.def_submodule("ttir");
  init_triton_passes_ttir(ttir);
  auto plugin = m.def_submodule("plugin");
  init_plugin_passes(plugin);
}
