/* Copyright 2023 The JAX Authors

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

#include "jaxlib/py_device_list.h"

#include <Python.h>

#include <cstddef>
#include <cstdint>
#include <optional>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "absl/container/inlined_vector.h"
#include "absl/hash/hash.h"
#include "absl/log/check.h"
#include "absl/status/statusor.h"
#include "absl/types/span.h"
#include "nanobind/make_iterator.h"
#include "nanobind/nanobind.h"
#include "nanobind/stl/set.h"  // IWYU pragma: keep
#include "nanobind/stl/string.h"  // IWYU pragma: keep
#include "nanobind/stl/string_view.h"  // IWYU pragma: keep
#include "jaxlib/nb_class_ptr.h"
#include "jaxlib/py_client.h"
#include "jaxlib/py_device.h"
#include "jaxlib/python_ref_manager.h"
#include "xla/pjrt/status_casters.h"
#include "xla/python/ifrt/device.h"
#include "xla/python/ifrt/device_list.h"
#include "xla/python/types.h"
#include "xla/util.h"

namespace jax {

namespace nb = ::nanobind;

PyDeviceList::PyDeviceList(nb_class_ptr<jax::PyClient> py_client,
                           xla::ifrt::DeviceListRef device_list)
    : py_client_(std::move(py_client)), device_list_(std::move(device_list)) {}

PyDeviceList::PyDeviceList(nb::tuple py_device_assignment)
    : device_list_(py_device_assignment) {
  // Attempt to convert to Python devices into `ifrt::DeviceList`.
  if (py_device_assignment.size() == 0) {
    return;
  }
  absl::InlinedVector<xla::ifrt::Device*, 1> devices;
  devices.reserve(py_device_assignment.size());
  for (nb::handle obj : py_device_assignment) {
    if (!nb::isinstance<jax::PyDevice>(obj.ptr())) {
      // Non-`jax::PyDevice` is used on an alternative JAX backend with device
      // duck typing. Use Python device objects already set in `device_list_`.
      return;
    }
    auto py_device = nb::cast<jax::PyDevice*>(obj);
    if (py_client_.get() == nullptr) {
      py_client_ = py_device->client();
    } else if (py_device->client().get() != py_client_.get()) {
      // If the list contains multiple clients, fall back to device duck typing.
      return;
    }
    devices.push_back(py_device->device());
  }
  device_list_ =
      xla::ValueOrThrow(py_client_->ifrt_client()->MakeDeviceList(devices));
}

PyDeviceList::~PyDeviceList() {
  if (device_list_.index() == 1) {
    xla::GlobalPyRefManager()->AddGarbage(
        std::move(std::get<1>(std::move(device_list_))));
  }
}

absl::StatusOr<xla::ifrt::DeviceListRef> PyDeviceList::ifrt_device_list()
    const {
  switch (device_list_.index()) {
    case 0:
      return std::get<0>(device_list_);
    case 1:
      return xla::InvalidArgument("DeviceList contains non-IFRT devices");
    default:
      return xla::InvalidArgument("Unrecognized DeviceList type");
  }
}

int64_t PyDeviceList::Hash() {
  if (!hash_.has_value()) {
    switch (device_list_.index()) {
      case 0:
        hash_ = absl::HashOf(std::get<0>(device_list_));
        break;
      case 1:
        hash_ = nb::hash(std::get<1>(device_list_));
        break;
      default:
        throw nb::value_error("Unrecognized DeviceList type");
    }
  }
  return *hash_;
}

/*static*/ bool PyDeviceList::Equal(nb_class_ptr<PyDeviceList> self,
                                    nb::handle other) {
  if (!nb::isinstance<PyDeviceList>(other)) {
    return false;
  }
  auto o = nb::cast<PyDeviceList*>(other);
  // Fast-path using a pointer equality check.
  if (self.get() == o) {
    return true;
  }
  int64_t h1, h2;
  {
    nb::ft_object_guard lock(self);
    h1 = self->Hash();
  }
  {
    nb::ft_object_guard lock(other);
    h2 = o->Hash();
  }
  if (h1 != h2) {
    return false;
  }
  if (self->device_list_.index() == 0 && o->device_list_.index() == 0) {
    nb::gil_scoped_release gil_release;
    return *std::get<0>(self->device_list_) == *std::get<0>(o->device_list_);
  } else {
    return self->AsTuple().equal(o->AsTuple());
  }
}

/*static*/ bool PyDeviceList::NotEqual(nb_class_ptr<PyDeviceList> self,
                                       nb::handle other) {
  return !Equal(std::move(self), other);
}

int PyDeviceList::Len() const {
  switch (device_list_.index()) {
    case 0:
      return std::get<0>(device_list_)->size();
    case 1:
      return nb::len(std::get<1>(device_list_));
    default:
      throw nb::value_error("Unrecognized DeviceList type");
  }
}

nb::object PyDeviceList::GetItem(int index) {
  switch (device_list_.index()) {
    case 0: {
      const xla::ifrt::DeviceListRef& device_list = std::get<0>(device_list_);
      if (index < -device_list->size() || index >= device_list->size()) {
        throw nb::index_error();
      } else if (index < 0) {
        index += device_list->size();
      }
      return py_client_->GetPyDevice(device_list->devices()[index]);
    }
    case 1:
      return std::get<1>(device_list_).attr("__getitem__")(index);
    default:
      throw nb::value_error("Unrecognized DeviceList type");
  }
}

nb::object PyDeviceList::GetSlice(nb::slice slice) {
  switch (device_list_.index()) {
    case 0: {
      const xla::ifrt::DeviceListRef& device_list = std::get<0>(device_list_);
      const absl::Span<xla::ifrt::Device* const> devices =
          device_list->devices();
      Py_ssize_t start, stop, step, slicelength;
      if (PySlice_GetIndicesEx(slice.ptr(), devices.size(), &start, &stop,
                               &step, &slicelength) != 0) {
        throw nb::python_error();
      }
      nb::tuple out = nb::steal<nb::tuple>(PyTuple_New(slicelength));
      for (size_t i = 0; i < slicelength; ++i) {
        nb::object d = py_client_->GetPyDevice(devices[start]);
        PyTuple_SET_ITEM(out.ptr(), i, d.release().ptr());
        start += step;
      }
      return std::move(out);
    }
    case 1:
      return std::get<1>(device_list_).attr("__getitem__")(slice);
    default:
      throw nb::value_error("Unrecognized DeviceList type");
  }
}

nb::tuple PyDeviceList::AsTuple() const {
  switch (device_list_.index()) {
    case 0: {
      const xla::ifrt::DeviceListRef& device_list = std::get<0>(device_list_);
      nb::tuple out = nb::steal<nb::tuple>(PyTuple_New(device_list->size()));
      int i = 0;
      for (xla::ifrt::Device* device : device_list->devices()) {
        nb::object d = py_client_->GetPyDevice(device);
        PyTuple_SET_ITEM(out.ptr(), i, d.release().ptr());
        ++i;
      }
      return out;
    }
    case 1:
      return std::get<1>(device_list_);
    default:
      throw nb::value_error("Unrecognized DeviceList type");
  }
}

nb::iterator PyDeviceList::Iter() {
  switch (device_list_.index()) {
    case 0: {
      // Iterator whose deference converts `xla::ifrt::Device*` into JAX
      // `PjRtDevice`.
      struct Iterator {
        void operator++() { ++it; }
        bool operator==(const Iterator& other) const { return it == other.it; }
        nb_class_ptr<jax::PyDevice> operator*() const {
          return py_client->GetPyDevice(*it);
        }
        nb_class_ptr<jax::PyClient> py_client;
        absl::Span<xla::ifrt::Device* const>::const_iterator it;
      };
      return nb::make_iterator(
          nb::type<PyDeviceList>(), "ifrt_device_iterator",
          Iterator{py_client_, std::get<0>(device_list_)->devices().cbegin()},
          Iterator{py_client_, std::get<0>(device_list_)->devices().cend()});
    }
    case 1:
      return nb::make_iterator(
          nb::type<PyDeviceList>(), "python_device_iterator",
          std::get<1>(device_list_).begin(), std::get<1>(device_list_).end());
    default:
      throw nb::value_error("Unrecognized DeviceList type");
  }
}

std::string PyDeviceList::Str() {
  return nb::cast<std::string>(nb::str(AsTuple()));
}

nb::tuple PyDeviceList::Dump() const { return AsTuple(); }

bool PyDeviceList::IsFullyAddressable() {
  if (!is_fully_addressable_.has_value()) {
    ProcessIndices();
    CHECK(process_indices_.has_value());
    if (process_indices_->size() > 1) {
      is_fully_addressable_ = false;
    } else {
      CHECK_EQ(process_indices_->size(), 1);
      int process_index;
      switch (device_list_.index()) {
        case 0: {
          process_index = py_client_ ? py_client_->process_index() : 0;
          break;
        }
        case 1: {
          process_index =
              nb::cast<int>(std::get<1>(device_list_)[0].attr("client").attr(
                  "process_index")());
          break;
        }
        default:
          throw nb::value_error("Unrecognized DeviceList type");
      }
      is_fully_addressable_ = *process_indices_->begin() == process_index;
    }
  }
  return *is_fully_addressable_;
}

/*static*/ nb_class_ptr<PyDeviceList> PyDeviceList::AddressableDeviceList(
    nb_class_ptr<PyDeviceList> self) {
  nb::ft_object_guard lock(self);
  if (self->IsFullyAddressable()) {
    // Do not cache this result in `addressable_device_list_`. Otherwise, it
    // will create a cycle that prevents deletion of this object.
    return self;
  }
  if (!self->addressable_device_list_.has_value()) {
    switch (self->device_list_.index()) {
      case 0: {
        absl::InlinedVector<xla::ifrt::Device*, 1> addressable_devices;
        const int process_index =
            self->py_client_ ? self->py_client_->process_index() : 0;
        for (xla::ifrt::Device* device :
             std::get<0>(self->device_list_)->devices()) {
          if (device->ProcessIndex() == process_index) {
            addressable_devices.push_back(device);
          }
        }
        self->addressable_device_list_ = make_nb_class<PyDeviceList>(
            self->py_client_,
            xla::ValueOrThrow(self->py_client_->ifrt_client()->MakeDeviceList(
                addressable_devices)));
        break;
      }
      case 1: {
        auto device_list = std::get<1>(self->device_list_);
        std::vector<nb::object> addressable_devices;
        for (size_t i = 0; i < device_list.size(); ++i) {
          nb::object device = device_list[i];
          if (nb::cast<int>(device.attr("process_index")) ==
              nb::cast<int>(device.attr("client").attr("process_index")())) {
            addressable_devices.push_back(std::move(device));
          }
        }
        self->addressable_device_list_ = make_nb_class<PyDeviceList>(
            xla::MutableSpanToNbTuple(absl::MakeSpan(addressable_devices)));
        break;
      }
      default:
        throw nb::value_error("Unrecognized DeviceList type");
    }
  }
  return nb::cast<nb_class_ptr<PyDeviceList>>(*self->addressable_device_list_);
}

const std::set<int>& PyDeviceList::ProcessIndices() {
  if (!process_indices_.has_value()) {
    process_indices_ = std::set<int>{};
    switch (device_list_.index()) {
      case 0: {
        for (const xla::ifrt::Device* device :
             std::get<0>(device_list_)->devices()) {
          process_indices_->insert(device->ProcessIndex());
        }
        break;
      }
      case 1: {
        for (nb::handle device : std::get<1>(device_list_)) {
          process_indices_->insert(nb::cast<int>(device.attr("process_index")));
        }
        break;
      }
      default:
        throw nb::value_error("Unrecognized DeviceList type");
    }
  }
  return *process_indices_;
}

const std::string& PyDeviceList::DeviceKind() {
  if (!device_kind_.has_value()) {
    auto device_list = ifrt_device_list();
    if (!device_list.ok()) {
      throw nb::value_error(device_list.status().ToString().c_str());
    }
    if (Len() == 0) {
      throw nb::value_error("DeviceList is empty");
    }
    device_kind_ = (*device_list)->devices()[0]->Kind();
  }
  return *device_kind_;
}

void PyDeviceList::PopulateMemoryKindInfo() {
  if (device_list_.index() == 1) {
    // Handle Python duck-type devices in a separate function for readability.
    PopulateMemoryKindInfoForDuckTypedDevices();
    return;
  }
  if (device_list_.index() != 0) {
    throw nb::value_error("Unrecognized DeviceList type");
  }
  MemoryKindInfo info;
  if (std::get<0>(device_list_)->size() == 0) {
    info.default_memory_kind = nb::none();
    memory_kind_info_ = std::move(info);
    return;
  }
  xla::ifrt::Device* device = std::get<0>(device_list_)->devices()[0];

  auto default_memory = device->DefaultMemory();
  if (!default_memory.ok()) {
    // Cache the error.
    memory_kind_info_ = default_memory.status();
    return;
  }
  info.default_memory_kind = nb::cast(*(*default_memory)->Kind().memory_kind());
  nb::tuple memory_kinds =
      nb::steal<nb::tuple>(PyTuple_New(device->Memories().size()));
  for (size_t i = 0; i < device->Memories().size(); ++i) {
    auto* memory = device->Memories()[i];
    nb::str s = nb::str(memory->Kind().memory_kind()->data(),
                        memory->Kind().memory_kind()->size());
    PyTuple_SET_ITEM(memory_kinds.ptr(), i, s.release().ptr());
  }
  info.memory_kinds = std::move(memory_kinds);
  memory_kind_info_ = std::move(info);
}

void PyDeviceList::PopulateMemoryKindInfoForDuckTypedDevices() {
  MemoryKindInfo info;
  try {
    if (std::get<1>(device_list_).size() == 0) {
      info.default_memory_kind = nb::none();
      // info.memory_kinds is default-initialized to an empty tuple.
      memory_kind_info_ = std::move(info);
      return;
    }
    nb::handle device = std::get<1>(device_list_)[0];
    auto default_memory = device.attr("default_memory")();
    info.default_memory_kind = default_memory.attr("kind");
    info.memory_kinds =
        nb::tuple(nb::object(device.attr("addressable_memories")()));
    memory_kind_info_ = std::move(info);
  } catch (nb::python_error& e) {
    // Cache the error.
    memory_kind_info_ = xla::InvalidArgument("%s", e.what());
  }
}

/*static*/ absl::StatusOr<nb::tuple> PyDeviceList::MemoryKinds(
    nb_class_ptr<PyDeviceList> self) {
  nb::ft_object_guard lock(self);
  if (!self->memory_kind_info_.has_value()) {
    self->PopulateMemoryKindInfo();
  }
  if (!self->memory_kind_info_->ok()) {
    return self->memory_kind_info_->status();
  }
  return (*self->memory_kind_info_)->memory_kinds;
}

/*static*/ absl::StatusOr<nb::object> PyDeviceList::DefaultMemoryKind(
    nb_class_ptr<PyDeviceList> self) {
  nb::ft_object_guard lock(self);
  if (!self->memory_kind_info_.has_value()) {
    self->PopulateMemoryKindInfo();
  }
  if (!self->memory_kind_info_->ok()) {
    return self->memory_kind_info_->status();
  }
  return (*self->memory_kind_info_)->default_memory_kind;
}

/*static*/ void PyDeviceList::Register(nb::module_& m) {
  nb::class_<PyDeviceList>(m, "DeviceList")
      .def(nb::init<nb::tuple>())
      .def("__hash__", &PyDeviceList::Hash, nb::lock_self())
      .def("__eq__", &PyDeviceList::Equal)
      .def("__ne__", &PyDeviceList::NotEqual)
      .def("__len__", &PyDeviceList::Len)
      .def("__getitem__", &PyDeviceList::GetItem)
      .def("__getitem__", &PyDeviceList::GetSlice)
      .def("__iter__", &PyDeviceList::Iter, nb::keep_alive<0, 1>())
      .def("__str__", &PyDeviceList::Str)
      .def("__repr__", &PyDeviceList::Str)
      .def("__getstate__", [](const PyDeviceList& l) { return l.Dump(); })
      .def("__setstate__",
           [](PyDeviceList& self, nb::tuple t) {
             new (&self) PyDeviceList(std::move(t));
           })
      .def_prop_ro("is_fully_addressable", &PyDeviceList::IsFullyAddressable,
                   nb::lock_self())
      .def_prop_ro("addressable_device_list",
                   &PyDeviceList::AddressableDeviceList)
      .def_prop_ro("process_indices", &PyDeviceList::ProcessIndices,
                   nb::lock_self())
      // `xla::ValueOrThrowWrapper` does not work with
      // `def_prop_ro()`. Manually convert an error into an exception.
      .def_prop_ro("default_memory_kind",
                   [](nb_class_ptr<PyDeviceList> l) {
                     auto kind = DefaultMemoryKind(l);
                     if (!kind.ok()) {
                       throw nb::value_error(kind.status().ToString().c_str());
                     }
                     return *kind;
                   })
      .def_prop_ro("memory_kinds",
                   [](nb_class_ptr<PyDeviceList> l) {
                     auto kinds = MemoryKinds(l);
                     if (!kinds.ok()) {
                       throw nb::value_error(kinds.status().ToString().c_str());
                     }
                     return *kinds;
                   })
      .def_prop_ro("device_kind", &PyDeviceList::DeviceKind, nb::lock_self());
}

}  // namespace jax
