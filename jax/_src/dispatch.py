# Copyright 2018 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Primitive dispatch and jit dispatch.
from __future__ import annotations

import atexit
from collections.abc import Sequence
import dataclasses
import enum
from functools import partial
import itertools
import logging
import threading
import time
from typing import Any

from jax._src import api
from jax._src import array
from jax._src import basearray
from jax._src import config
from jax._src import core
from jax._src import dtypes
from jax._src import lib
from jax._src import pjit
from jax._src import traceback_util
from jax._src import util

from jax._src import xla_bridge
from jax._src.abstract_arrays import array_types
from jax._src.interpreters import ad
from jax._src.interpreters import batching
from jax._src.interpreters import mlir
from jax._src.interpreters import pxla
from jax._src.interpreters import xla
from jax._src.api_util import InternalFloatingPointError
from jax._src.layout import Layout, Format
from jax._src.lib import xla_client as xc
from jax._src.mesh import AbstractMesh, Mesh
from jax._src.monitoring import record_scalar, record_event_duration_secs, record_event_time_span
from jax._src.partition_spec import PartitionSpec
from jax._src.sharding import Sharding
from jax._src.sharding_impls import (
    NamedSharding, SingleDeviceSharding, TransferToMemoryKind, GSPMDSharding,
    is_single_device_sharding)
from jax._src.stages import SourceInfo
import numpy as np


JAXPR_TRACE_EVENT = "/jax/core/compile/jaxpr_trace_duration"
JAXPR_TO_MLIR_MODULE_EVENT = "/jax/core/compile/jaxpr_to_mlir_module_duration"
BACKEND_COMPILE_EVENT = "/jax/core/compile/backend_compile_duration"

traceback_util.register_exclusion(__file__)

xe = xc._xla

Backend = xe.Client
Device = xc.Device

CompileOptions = xc.CompileOptions

map, unsafe_map = util.safe_map, map
zip, unsafe_zip = util.safe_zip, zip

logger = logging.getLogger(__name__)

# This flag is set on exit; no logging should be attempted
_on_exit = False

### op-by-op execution

def apply_primitive(prim, *args, **params):
  """Impl rule that compiles and runs a single primitive 'prim' using XLA."""
  fun = xla_primitive_callable(prim, **params)
  # TODO(yashkatariya): Investigate adding is_primitive to jit and never
  # triggering the disable jit path instead of messing around with it here.
  prev = lib.jax_jit.swap_thread_local_state_disable_jit(False)
  try:
    outs = fun(*args)
  finally:
    lib.jax_jit.swap_thread_local_state_disable_jit(prev)
  return outs

@util.cache()
def xla_primitive_callable(prim: core.Primitive, **params):
  util.test_event("xla_primitive_callable_cache_miss")
  def prim_fun(*args):
    with config.eager_constant_folding(False):
      return prim.bind(*args, **params)
  prim_fun.__name__ = prim.name
  prim_fun.__qualname__ = prim.name
  prim_fun._apply_primitive = True
  return api.jit(prim_fun)


def simple_impl(prim):
  prim.def_impl(partial(apply_primitive, prim))

RuntimeToken = Any

class RuntimeTokenSet(threading.local):
  """See docstring for effects.py module for the calling convention for tokens."""

  # For each ordered effect, the token returned by the last dispatched
  # computation, sharded over the devices in that computation.
  current_tokens: dict[core.Effect, core.Token]

  # For each device, the runtime token returned by the last dispatched
  # computation on that device.
  output_runtime_tokens: dict[Device, RuntimeToken]

  def __init__(self):
    self.current_tokens = {}
    self.output_runtime_tokens = {}

  def get_token_input(
      self, eff: core.Effect, devices: list[Device]
  ) -> core.Token:
    tok = self.current_tokens.get(eff, np.zeros(0, np.bool_))

    if isinstance(tok, core.Token):
      # The order of devices may change, so we need to reshard if necessary.
      # TODO(yueshengys): This might still be buggy in a multi-process SPMD
      # scenario. Revise the logic later. A distributed shutdown barrier inside
      # the XLA program may be needed.
      return api.device_put(
          tok, NamedSharding(Mesh(devices, 'x'), PartitionSpec('x')))

    # We only use replicated sharding for the first time when the token for the
    # order effect hasn't been created.
    s = GSPMDSharding.get_replicated(devices)
    sharded_tok = core.Token(pxla.shard_args([s], [None], [None], [tok])[0])
    self.current_tokens[eff] = sharded_tok
    return sharded_tok

  def set_token_result(self, eff: core.Effect, token: core.Token):
    self.current_tokens[eff] = token

  def set_output_runtime_token(self, device: Device, token: RuntimeToken):
    # We're free to clobber the previous output token because on each
    # device we have a total ordering of computations. Only the token
    # from the latest computation matters.
    self.output_runtime_tokens[device] = token

  def clear(self):
    self.current_tokens = {}
    self.output_runtime_tokens = {}

  def block_until_ready(self):
    for token in self.current_tokens.values():
      token.block_until_ready()
    for token in self.output_runtime_tokens.values():
      token.block_until_ready()
    self.clear()

runtime_tokens: RuntimeTokenSet = RuntimeTokenSet()

@atexit.register
def wait_for_tokens():
  runtime_tokens.block_until_ready()


class LogElapsedTimeContextManager:
  __slots__ = ['fmt', 'fun_name', 'event', 'start_time']

  def __init__(self, fmt: str, fun_name: str, event: str | None = None):
    self.fmt = fmt
    self.fun_name = fun_name
    self.event = event

  def __enter__(self):
    self.start_time = time.time()
    if self.event is not None:
      record_scalar(
          self.event, self.start_time, fun_name=self.fun_name
      )

  def __exit__(self, exc_type, exc_value, traceback):
    if _on_exit:
      return

    end_time = time.time()
    elapsed_time = end_time - self.start_time
    log_priority = logging.WARNING if config.log_compiles.value else logging.DEBUG
    if logger.isEnabledFor(log_priority):
      logger.log(log_priority, self.fmt.format(
          fun_name=self.fun_name, elapsed_time=elapsed_time))
    if self.event is not None:
      record_event_duration_secs(
          self.event, elapsed_time, fun_name=self.fun_name
      )
      record_event_time_span(
          self.event, self.start_time, end_time, fun_name=self.fun_name
      )

log_elapsed_time = LogElapsedTimeContextManager


def should_tuple_args(num_args: int, platform: str) -> bool:
  # CPU and GPU do not need tuples as they use host-side data structures that
  # do not have small bounds.
  # TPU only needs a tuple for very long lists
  if platform == "tpu":
    return num_args > 2000
  else:
    return False

def jaxpr_has_primitive(jaxpr: core.Jaxpr, prim_name: str) -> bool:
  """Whether there is a primitive given by user anywhere inside a Jaxpr."""
  for eqn in jaxpr.eqns:
    if prim_name in eqn.primitive.name:
      return True
  for subjaxpr in core.subjaxprs(jaxpr):
    if jaxpr_has_primitive(subjaxpr, prim_name):
      return True
  return False


# Use this registry with caution. It will void the guarantee that lowering to
# stablehlo is oblivious of physical devices.
prim_requires_devices_during_lowering: set[core.Primitive] = set()

@util.weakref_lru_cache
def jaxpr_has_prim_requiring_devices(jaxpr: core.Jaxpr) -> bool:
  for eqn in jaxpr.eqns:
    if eqn.primitive in prim_requires_devices_during_lowering:
      return True
  for subjaxpr in core.subjaxprs(jaxpr):
    if jaxpr_has_prim_requiring_devices(subjaxpr):
      return True
  return False


@util.weakref_lru_cache
def get_intermediate_shardings(
    jaxpr: core.Jaxpr) -> Sequence[tuple[Sharding, SourceInfo]]:
  from jax._src import shard_map  # pytype: disable=import-error

  out = []
  for eqn in jaxpr.eqns:
    if eqn.primitive is pjit.sharding_constraint_p:
      s = eqn.params['sharding']
      if isinstance(s, NamedSharding) and isinstance(s.mesh, AbstractMesh):
        continue
      source_info = SourceInfo(eqn.source_info, eqn.primitive.name)
      out.append((s, source_info))
    elif eqn.primitive is pjit.jit_p:
      source_info = SourceInfo(eqn.source_info, eqn.primitive.name)
      out.extend((i, source_info) for i in eqn.params['in_shardings'])
      out.extend((o, source_info) for o in eqn.params['out_shardings'])
    elif eqn.primitive is shard_map.shard_map_p:
      mesh = eqn.params['mesh']
      if isinstance(mesh, AbstractMesh):
        continue
      source_info = SourceInfo(eqn.source_info, eqn.primitive.name)
      out.extend((NamedSharding(mesh, spec), source_info)
                 for spec in [*eqn.params['in_specs'], *eqn.params['out_specs']])
    elif eqn.primitive is device_put_p:
      source_info = SourceInfo(eqn.source_info, eqn.primitive.name)
      out.extend((s, source_info) for s in eqn.params['devices']
                 if isinstance(s, Sharding) and s.memory_kind is not None)
  for subjaxpr in core.subjaxprs(jaxpr):
    out.extend(get_intermediate_shardings(subjaxpr))
  return out


def jaxpr_has_bints(jaxpr: core.Jaxpr) -> bool:
  return (any(type(v.aval.dtype) is core.bint for v in jaxpr.invars
              if isinstance(v.aval, core.UnshapedArray)) or
          any(_is_bint_axis_size(d)
              for j in itertools.chain([jaxpr], core.subjaxprs(jaxpr))
              for e in j.eqns for v in e.outvars
              if isinstance(v.aval, core.DShapedArray) for d in v.aval.shape))

def _is_bint_axis_size(d: core.AxisSize) -> bool:
  if isinstance(d, core.DArray):
    assert not d.shape
    return type(d.dtype) is core.bint
  elif isinstance(d, core.Var):
    return (isinstance(d.aval, core.DShapedArray) and
            type(d.aval.dtype) is core.bint)
  return False


def check_arg(arg: Any):
  if not (isinstance(arg, core.Tracer) or core.valid_jaxtype(arg)):
    raise TypeError(f"Argument '{arg}' of type {type(arg)} is not a valid "
                    "JAX type.")


def jaxpr_replicas(jaxpr: core.Jaxpr) -> int:
  """The number of replicas needed for a jaxpr.

  For a eqn, multiply the `axis_size` with the `jaxpr_replicas` of the
  subjaxprs. For a list of eqns, take the maximum number of replicas.
  """
  return max(unsafe_map(_eqn_replicas, jaxpr.eqns), default=1)

# TODO(mattjj): this function assumes that only pmap has a parameter named
# axis_size, and that it corresponds to cross-replica mapping
def _eqn_replicas(eqn: core.JaxprEqn) -> int:
  call_jaxpr = eqn.params.get("call_jaxpr")
  if call_jaxpr:
    return eqn.params.get('axis_size', 1) * jaxpr_replicas(call_jaxpr)
  elif eqn.primitive in xla.initial_style_primitives:
    return _initial_style_primitive_replicas(eqn.params)
  else:
    return 1

def _initial_style_primitive_replicas(params: dict[str, Any]) -> int:
  return max(core.traverse_jaxpr_params(jaxpr_replicas, params).values(),
             default=1)

def needs_check_special() -> bool:
  return config.debug_infs.value or config.debug_nans.value

def check_special(name: str, bufs: Sequence[basearray.Array]) -> None:
  if needs_check_special():
    for buf in bufs:
      _check_special(name, buf.dtype, buf)

def _check_special(name: str, dtype: np.dtype, buf: basearray.Array) -> None:
  if dtypes.issubdtype(dtype, np.inexact):
    if config.debug_nans.value and np.any(np.isnan(np.asarray(buf))):
      raise InternalFloatingPointError(name, "nan")
    if config.debug_infs.value and np.any(np.isinf(np.asarray(buf))):
      raise InternalFloatingPointError(name, "inf")

class CopySemantics(enum.Enum):
  ALIAS = enum.auto()
  COPY = enum.auto()
  DONATE = enum.auto()

def _identity_fn(x):
  return x

def _different_device_order_reshard(x, target_sharding, copy: CopySemantics):
  x._check_if_deleted()
  inp_sharding = x.sharding
  assert isinstance(inp_sharding, NamedSharding)

  donate_argnums = 0 if copy == CopySemantics.DONATE else None
  if inp_sharding._device_assignment == target_sharding._device_assignment:
    return api.jit(_identity_fn, out_shardings=target_sharding,
                   donate_argnums=donate_argnums)(x)

  if inp_sharding.is_fully_replicated:
    permute_order = None
  else:
    permute_order = np.vectorize(target_sharding._device_assignment.index,
                                  otypes=[int])(inp_sharding._device_assignment)
  new_mesh = Mesh(
      target_sharding.mesh.devices.reshape(inp_sharding.mesh.axis_sizes),
      inp_sharding.mesh.axis_names)
  new_s = NamedSharding(
      new_mesh, inp_sharding.spec, memory_kind=target_sharding.memory_kind,
      _logical_device_ids=(None if permute_order is None else
                            tuple(permute_order.tolist())))
  new_x = _reorder_shards(x, new_s, CopySemantics.ALIAS)
  return api.jit(_identity_fn, out_shardings=target_sharding,
                donate_argnums=donate_argnums)(new_x)


def _reorder_shards(x, new_s, copy_semantics: CopySemantics):
  """Reorders array shards to match the order indicated by the new sharding."""
  xc_copy_semantics = pxla.to_xc_copy_semantics([copy_semantics])[0]
  return xc.reorder_shards(x, new_s, xc_copy_semantics)  # type: ignore


@util.cache()
def _is_supported_cross_host_transfer(ndim, src_sharding, dst_sharding):
  """Returns True if src->dst is a supported cross-host transfer."""
  if xla_bridge.process_count() == 1:
    return False
  if (src_sharding._internal_device_list.device_kind !=
      dst_sharding._internal_device_list.device_kind):
    return False
  if (src_sharding._to_xla_hlo_sharding(ndim) !=
      dst_sharding._to_xla_hlo_sharding(ndim)):
    return False
  # This check excludes the case where the source and destination shardings
  # have the same process index sets but there are shards that require
  # cross-host transfers. This case is supportable but expensive to check for.
  different_process_inds = (
      src_sharding._internal_device_list.process_indices !=
      dst_sharding._internal_device_list.process_indices)
  backend = xla_bridge.get_backend()
  # If a cross-host device transfer is requested but the backend does not
  # support it, then the user must set the flags to enable DCN-based transfers.
  if (different_process_inds and
      not getattr(backend, 'supports_cross_host_transfers', False) and
      not xla_bridge.CROSS_HOST_TRANSFER_SOCKET_ADDRESS.value):
    raise ValueError(
        f"The backend ({backend.platform}, {backend.platform_version}) does "
        "not support cross-host device transfers via ICI/NCCL. Please set "
        "jax_cross_host_transfer_socket_address and (optionally) "
        "jax_cross_host_transport_addresses flags to enable DCN-based cross "
        "host device transfers.")
  return different_process_inds

@dataclasses.dataclass(frozen=True)
class _DeferredShardArg:
  """Deferred call to `pxla.shard_args`.

  Per-array impls return this object instead of a result array to indicate a
  deferred `shard_args` call. `_batched_device_put_impl` then batches all
  `_DeferredShardArg` objects into a single `shard_args` call.
  """

  x: Any
  s: Sharding
  aval: core.AbstractValue
  committed: bool
  copy_semantics: CopySemantics

  def result_handler(self, shard_arg_result):
    return pxla.global_aval_to_result_handler(
        self.aval, self.s, self.committed)(shard_arg_result)


def _device_put_sharding_impl(x, aval, device, copy):
  from jax.experimental import multihost_utils  # pytype: disable=import-error

  if isinstance(device, Sharding):
    s = device
    if (getattr(x, 'sharding', None) == s and getattr(x, '_committed', False)
        and copy == CopySemantics.ALIAS):
      return x

    if (not s.is_fully_addressable and
        isinstance(x, array.ArrayImpl) and not x.is_fully_addressable and
        s.device_set == x.sharding.device_set):
      assert isinstance(s, Sharding)
      return _different_device_order_reshard(x, s, copy)

    if (s.is_fully_addressable and isinstance(x, array.ArrayImpl) and
        x.is_fully_addressable and s.num_devices > 1 and
        s._internal_device_list != x.sharding._internal_device_list and  # pytype: disable=attribute-error
        s.device_set == x.sharding.device_set):
      assert isinstance(s, Sharding)
      return _different_device_order_reshard(x, s, copy)

    if (isinstance(x, array.ArrayImpl) and x._committed
        and _is_supported_cross_host_transfer(x.ndim, x.sharding, s)):
      return xc.batched_copy_array_to_devices_with_sharding(
          [x], [s._internal_device_list], [s],  # pytype: disable=attribute-error
          pxla.to_xc_copy_semantics([copy]))[0]

    if not s.is_fully_addressable:
      # If both the source and target shardings are not fully addressable and
      # one of the above conditions has not been met, then assume that the user
      # is attempting a different device order reshard.
      if (isinstance(x, array.ArrayImpl) and not x.is_fully_addressable
          and s.device_set != x.sharding.device_set):
        inp_ids = [d.id for d in x.sharding._device_assignment]
        inp_plat = x.sharding._device_assignment[0].platform.upper()
        target_ids = [d.id for d in s._device_assignment]
        target_plat = s._device_assignment[0].platform.upper()
        raise ValueError(
            "For a cross-host reshard in multi-controller JAX, input and target"
            " sharding should have the same set of devices. Got input's device"
            f" set ids: {inp_ids} on platform {inp_plat} and target sharding's"
            f" device set ids: {target_ids} on platform {target_plat}.\n\n"
            "There is experimental support for cross-host transfers with "
            "different device sets, when input/output shardings have the same "
            "indices and layouts, in the TFRT TPU runtime only.")

      if ((isinstance(x, array.ArrayImpl) and not x._committed) or
          type(x) in array_types or type(x) in dtypes.python_scalar_dtypes):
        # If all hosts participate in the sharding, assert that the input is the
        # same on all hosts. If some hosts have no addressable devices in the
        # sharding, bypass the check, since we can't easily distinguish between
        # these two cases: (1) the sharding contains the same subset of global
        # devices on all hosts (and hosts with no addressable devices in the
        # sharding do not transfer data) or (2) the sharding contains a
        # different subset of devices on each host. For (1), the input should be
        # the same on all hosts, but for (2) it need not be.
        if xla_bridge.process_count() == len(s._internal_device_list.process_indices):  # pytype: disable=attribute-error
          multihost_utils.assert_equal(
              x, fail_message=(
                  f"{type(x)} passed to device_put is not the same on each"
                  " process. Make sure you are passing the same value of"
                  f" {type(x)} on each process."))
        return _DeferredShardArg(x, s, aval, True, copy)
      # TODO(yashkatariya,mattjj): Link to a doc about McJAX and jax.Array.
      raise ValueError(
          "device_put's second argument must be a Device or a Sharding which"
          f" represents addressable devices, but got {s}. Please pass device or"
          " Sharding which represents addressable devices.")
    return _DeferredShardArg(x, s, aval, True, copy)

  # Only `Device` exists below. `Sharding` instance is handled above.
  if isinstance(x, array.ArrayImpl):
    if not x.is_fully_addressable:
      raise ValueError(
          "device_put's first argument must be a fully addressable array, but "
          f"got value with devices {x.devices()}")
    if device is None:
      if copy == CopySemantics.ALIAS:
        return x
      else:
        return _DeferredShardArg(x, x.sharding, aval, x.committed, copy)
    elif is_single_device_sharding(x.sharding):
      device = x.sharding._device_assignment[0] if device is None else device
      if copy == CopySemantics.COPY:
        return xc.batched_device_put(aval, SingleDeviceSharding(device), [x],
                                     [device], True, True)
      return pxla.batched_device_put(aval, SingleDeviceSharding(device), [x],
                                     [device])

  sh = SingleDeviceSharding(pxla.get_default_device()
                            if device is None else device)
  return _DeferredShardArg(x, sh, aval, device is not None, copy)


def _device_put_impl(
    x, *, device: Device | Sharding | Format | None,
    src: Device | Sharding | Format | None, copy: CopySemantics):
  if (isinstance(device, TransferToMemoryKind) or
      isinstance(src, TransferToMemoryKind)):
    raise ValueError(
        "TransferToMemoryKind argument to jax.device_put can only be used"
        " inside jax.jit. If you are using device_put outside jax.jit, then"
        " please provide a concrete Sharding with memory_kind.")

  try:
    aval = core.abstractify(x)
  except TypeError as err:
    raise TypeError(
        f"Argument '{x}' of type {type(x)} is not a valid JAX type") from err

  if isinstance(device, Format):
    l = device
    dll = l.layout
    x_dll = x.format.layout if hasattr(x, 'format') else None
    if dll is None and l.sharding is None:
      return _device_put_sharding_impl(x, aval, l.sharding, copy)
    if (not isinstance(l.sharding, Sharding) or
        not isinstance(dll, (Layout, type(None)))):
      raise ValueError(
          "sharding and layout in `Layout` instance should be"
          f" concrete. Got layout: {l} for input {aval.str_short()}")
    if (getattr(x, 'format', None) == l and getattr(x, '_committed', False) and
        copy == CopySemantics.ALIAS):
      return x
    if x_dll is None and dll is None:
      return _device_put_sharding_impl(x, aval, l.sharding, copy)
    return api.jit(
        _identity_fn, out_shardings=l,
        donate_argnums=(0 if copy == CopySemantics.DONATE else None))(x)

  return _device_put_sharding_impl(x, aval, device, copy)


def _batched_device_put_impl(
    *xs,
    devices: Sequence[Device | Sharding | Format | None],
    srcs: Sequence[Device | Sharding | Format | None],
    copy_semantics: Sequence[CopySemantics]):
  ys = []
  dsa_indices, dsa_xs, dsa_shardings, dsa_copy_semantics = [], [], [], []
  for i, (x, device, src, cp) in enumerate(zip(xs, devices, srcs, copy_semantics)):
    y = _device_put_impl(x, device=device, src=src, copy=cp)
    if isinstance(y, _DeferredShardArg):
      dsa_indices.append(i)
      dsa_xs.append(y.x)
      dsa_shardings.append(y.s)
      dsa_copy_semantics.append(y.copy_semantics)
    ys.append(y)

  if dsa_xs:
    # Batch shard_arg calls. Helps improve efficiency for backends that support
    # efficient batch transfer.
    # device_put handles `Format` via a different path, so just pass `None` as
    # the layout here.
    shard_arg_results = pxla.shard_args(dsa_shardings, [None] * len(dsa_xs),
                                        dsa_copy_semantics, dsa_xs)
    for i, shard_arg_result in zip(dsa_indices, shard_arg_results):
      assert isinstance(ys[i], _DeferredShardArg)
      ys[i] = ys[i].result_handler(shard_arg_result)

  return ys


device_put_p = core.Primitive('device_put')
device_put_p.multiple_results = True
device_put_p.def_impl(_batched_device_put_impl)

valid_memory_kinds = {'device', 'pinned_host', 'unpinned_host'}

def _device_put_abstract_eval(*xs, devices, srcs, copy_semantics):
  out = []
  for x, d in zip(xs, devices):
    if (isinstance(d, (Sharding, TransferToMemoryKind)) and
        d.memory_kind is not None):
      # TODO(yashkatariya): Maybe move this to `mem_kind_to_space`?
      if d.memory_kind not in valid_memory_kinds:
        raise ValueError(f'Got invalid memory_kind: {d.memory_kind}')
      out.append(x.update(memory_space=core.mem_kind_to_space(d.memory_kind)))
    else:
      out.append(x)
  return out
device_put_p.def_abstract_eval(_device_put_abstract_eval)

def _device_put_transpose(cts, *_, devices, srcs, copy_semantics):
  results = [None] * len(cts)
  dp_args = []
  for i, (ct, device, src, cp) in enumerate(zip(cts, devices, srcs, copy_semantics)):
    if type(ct) is not ad.Zero:
      dp_args.append((i, ct, device, src, cp))
  if dp_args:
    indices, args, devices, srcs, copy_semantics = list(zip(*dp_args))
    new_copy_semantics = []
    for cp in copy_semantics:
      if cp == CopySemantics.DONATE:
        raise ValueError(
            "donate=True is not allowed during tranposition of device_put."
            " Please file an issue if you want this to be supported.")
      elif cp == CopySemantics.ALIAS:
        new_copy_semantics.append(CopySemantics.COPY)
      else:
        assert cp == CopySemantics.COPY
        new_copy_semantics.append(CopySemantics.COPY)
    ys = device_put_p.bind(*args, devices=srcs, srcs=devices,
                           copy_semantics=tuple(new_copy_semantics))
    for i, y in zip(indices, ys):
      results[i] = y
  return results
ad.primitive_jvps[device_put_p] = partial(ad.linear_jvp, device_put_p)
ad.primitive_transposes[device_put_p] = _device_put_transpose

def _device_put_batcher(batched_args, batch_dims, **params):
  mapped_batch_dims = [bd for bd in batch_dims if bd is not batching.not_mapped]
  assert not mapped_batch_dims or all(
      mapped_batch_dims[0] == bd for bd in mapped_batch_dims[1:]
  ), batch_dims
  return device_put_p.bind(*batched_args, **params), batch_dims
batching.primitive_batchers[device_put_p] = _device_put_batcher

def _tpu_gpu_device_put_lowering(ctx, *xs, devices, srcs, copy_semantics):
  # TODO(yashkatariya): Maybe we should add the custom calls anyways if it's
  # being used inside jit? Atleast for now, this preserves the old behavior.
  if ctx.module_context.all_default_mem_kind:
    return xs
  def lower(x, device, aval, out_aval):
    if (isinstance(device, (Sharding, TransferToMemoryKind)) and
        device.memory_kind is not None):
      if isinstance(device, Sharding):
        if config.use_shardy_partitioner.value:
          x = mlir.wrap_with_sharding_op(
              ctx, x, out_aval,
              device._to_sdy_sharding(aval.ndim))
        else:
          x = mlir.wrap_with_sharding_op(
              ctx, x, out_aval,
              device._to_xla_hlo_sharding(aval.ndim).to_proto())
      x = mlir.wrap_with_memory_kind(x, device.memory_kind, out_aval)
      return x
    return x
  return list(map(lower, xs, devices, ctx.avals_in, ctx.avals_out))

mlir.register_lowering(
  device_put_p, _tpu_gpu_device_put_lowering, platform='tpu')
mlir.register_lowering(
  device_put_p, _tpu_gpu_device_put_lowering, platform='gpu')


def _common_device_put_lowering(ctx, *xs, devices, srcs, copy_semantics):
  return xs
mlir.register_lowering(device_put_p, _common_device_put_lowering)
