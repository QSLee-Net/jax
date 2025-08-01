/* Copyright 2024 The JAX Authors

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
==============================================================================*/

#ifndef JAXLIB_PY_DEVICE_H_
#define JAXLIB_PY_DEVICE_H_

#include <Python.h>

#include <cstdint>
#include <optional>

#include "absl/status/status.h"
#include "absl/status/statusor.h"
#include "absl/strings/string_view.h"
#include "nanobind/nanobind.h"
#include "jaxlib/nb_class_ptr.h"
#include "jaxlib/py_client.h"
#include "xla/literal.h"
#include "xla/python/ifrt/device.h"
#include "xla/shape.h"

namespace jax {

class PyDevice {
 public:
  PyDevice(nb_class_ptr<PyClient> client, xla::ifrt::Device* device);

  // Devices are compared using Python object identity, so we don't allow them
  // to be copied or moved.
  PyDevice(const PyDevice&) = delete;
  PyDevice(PyDevice&&) = delete;
  PyDevice& operator=(const PyDevice&) = delete;
  PyDevice& operator=(PyDevice&&) = delete;

  const nb_class_ptr<PyClient>& client() const { return client_; }
  xla::ifrt::Device* device() const { return device_; }

  int id() const;
  int process_index() const;
  absl::string_view platform() const;
  absl::string_view device_kind() const;
  std::optional<int> local_hardware_id() const;

  absl::string_view Str() const;
  absl::string_view Repr() const;

  absl::StatusOr<nb_class_ptr<PyMemorySpace>> Memory(
      absl::string_view kind) const;
  absl::StatusOr<nb_class_ptr<PyMemorySpace>> DefaultMemory() const;
  nanobind::list AddressableMemories() const;
  absl::StatusOr<std::optional<nanobind::dict>> MemoryStats() const;

  absl::StatusOr<std::intptr_t> GetStreamForExternalReadyEvents() const;

  static void RegisterPythonType(nanobind::module_& m);

 private:
  static int tp_traverse(PyObject* self, visitproc visit, void* arg);
  static int tp_clear(PyObject* self);
  static PyType_Slot slots_[];

  nb_class_ptr<PyClient> client_;
  xla::ifrt::Device* device_;
};

}  // namespace jax

#endif  // JAXLIB_PY_DEVICE_H_
