cmake_minimum_required(VERSION 3.20)

if(POLICY CMP0116)
  cmake_policy(SET CMP0116 OLD)
endif()

project(triton-anchor LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_INCLUDE_CURRENT_DIR ON)

set(TRITON_VERSION "3.8.0" CACHE STRING "Vendored Triton version")
option(TRITON_BUILD_PYTHON_MODULE "Build the Triton Python extension" ON)
option(TRITON_EXT_ENABLED
       "Export Triton and LLVM symbols required by out-of-tree extensions" ON)

set(UPSTREAM_DIR "${CMAKE_CURRENT_SOURCE_DIR}/triton")
set(TRITON_SOURCE_DIR "${UPSTREAM_DIR}")
set(TRITON_BINARY_DIR "${CMAKE_CURRENT_BINARY_DIR}/triton")

if(NOT MLIR_DIR AND LLVM_LIBRARY_DIR)
  set(MLIR_DIR "${LLVM_LIBRARY_DIR}/cmake/mlir")
endif()

find_package(MLIR REQUIRED CONFIG PATHS ${MLIR_DIR} NO_DEFAULT_PATH)
list(APPEND CMAKE_MODULE_PATH "${MLIR_CMAKE_DIR}")
list(APPEND CMAKE_MODULE_PATH "${LLVM_CMAKE_DIR}")

include(TableGen)
include(AddLLVM)
include(AddMLIR)

find_package(Python3 REQUIRED COMPONENTS Development.Module Interpreter)

# nanobind's CMake package expects the non-versioned Python target names.
if(NOT TARGET Python::Interpreter)
  add_executable(Python::Interpreter ALIAS Python3::Interpreter)
endif()
if(NOT TARGET Python::Module)
  add_library(Python::Module ALIAS Python3::Module)
  set(Python_EXECUTABLE "${Python3_EXECUTABLE}")
  set(Python_INCLUDE_DIRS "${Python3_INCLUDE_DIRS}")
  set(Python_SOABI "${Python3_SOABI}")
  set(Python_VERSION "${Python3_VERSION}")
  set(Python_INTERPRETER_ID "${Python3_INTERPRETER_ID}")
endif()

find_package(nanobind CONFIG REQUIRED HINTS "${Python3_SITELIB}")
nanobind_build_library(nanobind-static)
target_link_libraries(nanobind-static PRIVATE Python3::Module)

if(NOT MSVC)
  set(TRITON_DISABLE_EH_RTTI_FLAGS
      "$<$<COMPILE_LANGUAGE:CXX>:-fno-exceptions;-fno-rtti>")
  set(CMAKE_CXX_FLAGS
      "${CMAKE_CXX_FLAGS} -D__STDC_FORMAT_MACROS -fPIC -std=gnu++17 -Werror -Wno-covered-switch-default")
  if(TRITON_EXT_ENABLED)
    add_compile_definitions(TRITON_EXT_ENABLED=1)
  else()
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -fvisibility=hidden")
  endif()
else()
  set(CMAKE_CXX_FLAGS
      "${CMAKE_CXX_FLAGS} /wd4244 /wd4624 /wd4715 /wd4530")
endif()

set(CMAKE_C_FLAGS_TRITONRELBUILDWITHASSERTS "-O2 -g")
set(CMAKE_CXX_FLAGS_TRITONRELBUILDWITHASSERTS "-O2 -g")
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE "Release")
endif()

if(NOT WIN32 AND NOT APPLE AND NOT BSD)
  link_libraries(stdc++fs)
endif()

set_property(GLOBAL PROPERTY TRITON_LIBS "")
function(add_triton_object name)
  cmake_parse_arguments(ARG "" "" "DEPENDS;LINK_LIBS" ${ARGN})
  add_library(${name} OBJECT)
  target_sources(${name}
    PRIVATE ${ARG_UNPARSED_ARGUMENTS}
    INTERFACE $<TARGET_OBJECTS:${name}>
  )
  if(ARG_DEPENDS)
    add_dependencies(${name} ${ARG_DEPENDS})
  endif()
  if(ARG_LINK_LIBS)
    target_link_libraries(${name} PUBLIC ${ARG_LINK_LIBS})
  endif()
endfunction()

function(add_triton_library name)
  set_property(GLOBAL APPEND PROPERTY TRITON_LIBS ${name})
  add_triton_object(${name} ${ARGN})
  target_compile_options(${name} PRIVATE ${TRITON_DISABLE_EH_RTTI_FLAGS})
endfunction()

set_property(GLOBAL PROPERTY TRITON_PLUGINS "")
function(add_triton_plugin name)
  set_property(GLOBAL APPEND PROPERTY TRITON_PLUGINS ${name})
  add_triton_object(${name} ${ARGN})
endfunction()

include_directories(${MLIR_INCLUDE_DIRS})
include_directories(${LLVM_INCLUDE_DIRS})
include_directories(${Python3_INCLUDE_DIRS})
include_directories(${UPSTREAM_DIR})
include_directories(${UPSTREAM_DIR}/include)
include_directories(${TRITON_BINARY_DIR}/include)
include_directories(${UPSTREAM_DIR}/third_party)
include_directories(${TRITON_BINARY_DIR}/third_party)
include_directories(${CMAKE_CURRENT_SOURCE_DIR}/csrc/include)
include_directories(${CMAKE_CURRENT_BINARY_DIR}/csrc/include)

file(MAKE_DIRECTORY "${TRITON_BINARY_DIR}/include/triton")
configure_file(
  "${UPSTREAM_DIR}/include/triton/Version.h.in"
  "${TRITON_BINARY_DIR}/include/triton/Version.h"
  @ONLY
)

# Build only the hardware-independent TTIR dialect and transforms.
# TritonGPU, NVIDIA, AMD, Proton and their code generation live in OOT plugins.
add_subdirectory(triton/include/triton/Dialect/Triton)
add_subdirectory(triton/lib/Dialect/Triton)

# Keep only the generic OOT plugin loader from upstream Tools.  Building the
# complete TritonTools target would also pull in GPU layout/swizzling helpers.
add_triton_library(TritonPluginUtils
  ${UPSTREAM_DIR}/lib/Tools/PluginUtils.cpp

  LINK_LIBS PUBLIC
  MLIRIR
)

# Anchor-owned, target-independent lowering dialects and passes.
get_property(TRITON_CORE_LIBRARIES GLOBAL PROPERTY TRITON_LIBS)
add_subdirectory(csrc)
get_property(TRITON_ALL_LIBRARIES GLOBAL PROPERTY TRITON_LIBS)
set(TRITON_ANCHOR_LIBRARIES ${TRITON_ALL_LIBRARIES})
list(REMOVE_ITEM TRITON_ANCHOR_LIBRARIES ${TRITON_CORE_LIBRARIES})
if(NOT MSVC)
  foreach(target ${TRITON_ANCHOR_LIBRARIES})
    if(TARGET ${target})
      target_compile_options(${target} PRIVATE -Wno-deprecated-declarations)
    endif()
  endforeach()
endif()

if(TRITON_BUILD_PYTHON_MODULE)
  set(PYTHON_SRC_PATH "${UPSTREAM_DIR}/python/src")
  include_directories(${PYTHON_SRC_PATH})

  # Upstream ir.cc/passes.cc/main.cc/llvm.cc directly bind TritonGPU,
  # TritonNvidiaGPU, Gluon and vendor target initialization.  Keep the
  # vendored upstream sources untouched, but compile Anchor-owned core-only
  # bindings for a hardware-independent Wheel.
  set(TRITON_CORE_BINDING_SOURCES
    csrc/bindings/triton_core_main.cc
    csrc/bindings/triton_core_ir.cc
    csrc/bindings/triton_core_passes.cc
    csrc/bindings/triton_core_llvm.cc
  )

  get_property(TRITON_LIBRARIES GLOBAL PROPERTY TRITON_LIBS)

  list(APPEND TRITON_LIBRARIES
    MLIRIR
    MLIRArithToLLVM
    MLIRBuiltinToLLVMIRTranslation
    MLIRControlFlowToLLVM
    MLIRPass
    MLIRTransforms
    MLIRLLVMDialect
    MLIRLLVMToLLVMIRTranslation
    MLIRLinalgUtils
    MLIRSupport
    MLIRTargetLLVMIRExport
    MLIRMathToLLVM
    MLIRSCFToControlFlow
    MLIRIndexToLLVM
    MLIRReconcileUnrealizedCasts
    MLIRUBToLLVM
    LLVMPasses
    LLVMPlugins
    LLVMMIRParser
    Python3::Module
    nanobind-static
  )

  if(CMAKE_SYSTEM_PROCESSOR MATCHES "aarch64|arm64")
    list(APPEND TRITON_LIBRARIES LLVMAArch64CodeGen LLVMAArch64AsmParser)
  elseif(CMAKE_SYSTEM_PROCESSOR MATCHES "x86_64|AMD64")
    list(APPEND TRITON_LIBRARIES LLVMX86CodeGen LLVMX86AsmParser)
  elseif(CMAKE_SYSTEM_PROCESSOR MATCHES "riscv64")
    list(APPEND TRITON_LIBRARIES LLVMRISCVCodeGen LLVMRISCVAsmParser)
  else()
    message(FATAL_ERROR "Unsupported host architecture: ${CMAKE_SYSTEM_PROCESSOR}")
  endif()

  add_library(triton SHARED
    ${TRITON_CORE_BINDING_SOURCES}
    ${PYTHON_SRC_PATH}/interpreter.cc
    ${PYTHON_SRC_PATH}/specialize.cc
    csrc/bindings/triton_anchor_passes.cc
  )

  if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    target_compile_options(triton PRIVATE -Wno-array-bounds)
  endif()

  target_link_libraries(triton PRIVATE ${TRITON_LIBRARIES})
  set_target_properties(triton PROPERTIES INTERFACE_LINK_LIBRARIES "")

  if(WIN32)
    target_link_libraries(triton PRIVATE ${CMAKE_DL_LIBS})
  else()
    target_link_libraries(triton PRIVATE z)
  endif()
  target_link_options(triton PRIVATE ${LLVM_LDFLAGS})

  if(NOT WIN32)
    set_target_properties(triton PROPERTIES SUFFIX ".so")
    if(APPLE)
      target_link_options(triton PRIVATE "-undefined" "dynamic_lookup")
    endif()
  endif()

  set_target_properties(triton PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_LIBRARY_OUTPUT_DIRECTORY}/triton/_C"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_LIBRARY_OUTPUT_DIRECTORY}/triton/_C"
  )
endif()
