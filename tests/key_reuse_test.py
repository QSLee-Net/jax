# Copyright 2023 The JAX Authors.
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

from absl.testing import absltest, parameterized
from functools import partial

import numpy as np
import jax
from jax import core
import jax.numpy as jnp
from jax._src import prng
from jax._src import test_util as jtu
from jax.experimental.key_reuse._common import (
  assert_consumed, assert_unconsumed, consume, consume_p, unconsumed_copy_p)
from jax.experimental.key_reuse import (
  _forwarding, _simple, KeyReuseError, unconsumed_copy)

from jax import config
config.parse_flags_with_absl()


key = jax.eval_shape(jax.random.key, 0)
key1D = jax.eval_shape(lambda key: key[None], key)


primitives_with_static_signatures = {
  consume_p: (consume, key),
  unconsumed_copy_p: (unconsumed_copy, key),
  prng.random_bits_p: (jax.random.bits, key),
  prng.random_fold_in_p: (jax.random.fold_in, key, 2),
  prng.random_seed_p: (jax.random.key, 0),
  prng.random_split_p: (jax.random.split, key),
  prng.random_wrap_p: (jax.random.wrap_key_data, np.uint32([0, 0])),
  prng.random_unwrap_p: (jax.random.key_data, key),
  jax.random.random_gamma_p: (jax.random.gamma, key, 1.0),
  jax.lax.broadcast_in_dim_p: (lambda key: key[None], key),
  jax.lax.copy_p: (jnp.array, key),
  jax.lax.convert_element_type_p: (lambda key: jnp.array(key, dtype=key.dtype), key),
  jax.lax.device_put_p: (jax.device_put, key),
  jax.lax.reshape_p: (lambda key: key.reshape((1,)), key),
  jax.lax.squeeze_p: (jnp.squeeze, key1D),
  jax.lax.dynamic_slice_p: (partial(jax.lax.dynamic_slice, slice_sizes=(1,)), key1D, (0,)),
  jax.lax.dynamic_update_slice_p: (jax.lax.dynamic_update_slice, key1D, key1D, (0,)),
}

# Primitive that is unknown to the key reuse machinery
unknown_p = core.Primitive("unknown")
unknown_p.def_abstract_eval(lambda x: x)
unknown_p.def_impl(lambda x: x)
def apply_unknown_primitive(key):
  return unknown_p.bind(key)


@jtu.with_config(
  jax_enable_custom_prng=False,
  jax_enable_key_reuse_checks=False)
class KeyReuseUnitTestSimple(jtu.JaxTestCase):
  def check_key_reuse(self, *args):
    return _simple.check_key_reuse(*args)

  def test_assertions(self):
    key = jax.random.key(0)
    self.check_key_reuse(assert_unconsumed, key)
    with self.assertRaises(AssertionError):
      self.check_key_reuse(assert_consumed, key)

  def test_unknown(self):
    def f(key):
      assert_unconsumed(key)
      key2 = apply_unknown_primitive(key)
      assert_consumed(key)
      assert_consumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_consume(self):
    def f(key):
      assert_unconsumed(key)
      key2 = consume(key)
      assert_consumed(key)
      assert_consumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_unconsumed_copy(self):
    def f(key):
      assert_unconsumed(key)
      consume(key)
      assert_consumed(key)
      key2 = unconsumed_copy(key)
      assert_unconsumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_seed(self):
    def f():
      key = jax.random.key(0)
      assert_unconsumed(key)
    self.check_key_reuse(f)

  def test_split(self):
    def f(key):
      assert_unconsumed(key)
      key2 = jax.random.split(key)
      assert_unconsumed(key2)
      assert_consumed(key)
    self.check_key_reuse(f, jax.random.key(0))

  def test_fold_in(self):
    def f(key):
      assert_unconsumed(key)
      key2 = jax.random.fold_in(key, 2)
      assert_unconsumed(key)
      assert_unconsumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_bits(self):
    def f(key):
      assert_unconsumed(key)
      bits = jax.random.bits(key, (), 'uint32')
      assert_consumed(key)
      return bits
    self.check_key_reuse(f, jax.random.key(0))

  def test_wrap(self):
    def f(key_data):
      key = jax.random.wrap_key_data(key_data)
      assert_unconsumed(key)
    self.check_key_reuse(f, jax.random.PRNGKey(0))

  def test_unwrap(self):
    def f(key):
      assert_unconsumed(key)
      key_data = jax.random.key_data(key)
      assert_consumed(key)
    self.check_key_reuse(f, jax.random.key(0))

  def test_gamma(self):
    def f(key):
      assert_unconsumed(key)
      values = jax.random.gamma(key, 1.0)
      assert_consumed(key)
      return values
    self.check_key_reuse(f, jax.random.key(0))

  def test_broadcast_in_dim(self):
    def f(key):
      assert_unconsumed(key)
      key2 = key[None]
      assert_consumed(key)
      assert_unconsumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_copy(self):
    def f(key):
      assert_unconsumed(key)
      key2 = jnp.array(key, copy=True)
      assert_consumed(key)
      assert_unconsumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_device_put(self):
    def f(key):
      assert_unconsumed(key)
      key2 = jax.device_put(key)
      assert_consumed(key)
      assert_unconsumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_squeeze(self):
    def f(key):
      assert_unconsumed(key)
      key2 = jax.lax.squeeze(key, (0,))
      assert_consumed(key)
      assert_unconsumed(key2)
    self.check_key_reuse(f, jax.random.key(0)[None])

  def test_reshape(self):
    def f(key):
      assert_unconsumed(key)
      key2 = key.reshape(1, *key.shape)
      assert_consumed(key)
      assert_unconsumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_slice(self):
    def f(keys):
      assert_unconsumed(keys)

      assert_unconsumed(keys[0])
      assert_consumed(keys, np.array([True, False]))

      assert_unconsumed(keys[1])
      assert_consumed(keys, np.array([True, True]))
    self.check_key_reuse(f, jax.random.split(jax.random.key(0)))

  def test_jit_can_consume_input(self):
    def f(key):
      assert_unconsumed(key)
      jax.jit(jax.random.bits)(key)
      assert_consumed(key)
    self.check_key_reuse(f, jax.random.key(0))

  def test_jit_can_return_consumed_output(self):
    def f():
      def g():
        key = jax.random.key(0)
        assert_unconsumed(key)
        bits = jax.random.bits(key)
        assert_consumed(key)
        return bits, key
      _, key = jax.jit(g)()
      assert_consumed(key)
    self.check_key_reuse(f)

  def test_jit_duplicate_inputs(self):
    def f(key):
      assert_unconsumed(key)
      def g(key1, key2):
        return jax.random.bits(key1)
      _ = jax.jit(g)(key, key)
      assert_consumed(key)
    # TODO(jakevdp) handle this somehow?
    with self.assertRaisesRegex(ValueError, "pjit with duplicate inputs"):
      self.check_key_reuse(f, jax.random.key(0))

  def test_jit_propagates_consumption_bit(self):
    def f(key):
      assert_unconsumed(key)
      g = jax.jit(lambda: key)
      key2 = g()
      assert_consumed(key)
      assert_unconsumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_jit_duplicate_outputs(self):
    # TODO(jakevdp): implement this case
    def f(key):
      assert_unconsumed(key)
      def g(key):
        return key, key
      key1, key2 = jax.jit(g)(key)
      assert_consumed(key)
      assert_unconsumed(key1)
      assert_consumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_cond_source(self):
    @jax.jit
    def f(flag, key):
      f1 = lambda seed, _: jax.random.key(seed)
      f2 = lambda _, key: key
      key_out = jax.lax.cond(flag, f1, f2, 0, key)
      assert_unconsumed(key_out)
    self.check_key_reuse(f, True, jax.random.key(0))

  def test_cond_both_consumed(self):
    @jax.jit
    def f(flag, key):
      assert_unconsumed(key)
      _ = jax.lax.cond(
        flag, jax.random.uniform, jax.random.normal, key)
      assert_consumed(key)
    self.check_key_reuse(f, True, jax.random.key(0))

  def test_cond_one_consumed(self):
    @jax.jit
    def f(flag, key):
      assert_unconsumed(key)
      _ = jax.lax.cond(
        flag, jax.random.uniform, lambda k: 1.0, key)
      assert_consumed(key)
    self.check_key_reuse(f, True, jax.random.key(0))

  def test_cond_neither_consumed(self):
    @jax.jit
    def f(flag, key):
      assert_unconsumed(key)
      _ = jax.lax.cond(
        flag, lambda k: 0.0, lambda k: 1.0, key)
      assert_unconsumed(key)
    self.check_key_reuse(f, True, jax.random.key(0))

  def test_simple_vmap(self):
    @jax.jit
    def f(seed):
      key = jax.random.key(seed)
      assert_unconsumed(key)
      result = jax.random.uniform(key)
      assert_consumed(key)
      return result
    self.check_key_reuse(f, 0)
    self.check_key_reuse(jax.vmap(f), jnp.arange(4))

  @parameterized.parameters(*primitives_with_static_signatures)
  def test_jaxpr_type_signature(self, primitive):
    func, *args = primitives_with_static_signatures[primitive]
    signature = _simple.key_reuse_signatures[primitive]
    jaxpr = jax.make_jaxpr(func)(*args)
    self.assertEqual(signature, _simple.get_jaxpr_type_signature(jaxpr.jaxpr))


@jtu.with_config(
  jax_enable_custom_prng=False,
  jax_enable_key_reuse_checks=False)
class KeyReuseUnitTestWithForwarding(jtu.JaxTestCase):
  def check_key_reuse(self, *args):
    return _forwarding.check_key_reuse(*args)

  def test_assertions(self):
    key = jax.random.key(0)
    self.check_key_reuse(assert_unconsumed, key)
    with self.assertRaises(AssertionError):
      self.check_key_reuse(assert_consumed, key)

  def test_unknown(self):
    def f(key):
      assert_unconsumed(key)
      key2 = apply_unknown_primitive(key)
      assert_consumed(key)
      assert_consumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_consume(self):
    def f(key):
      assert_unconsumed(key)
      key2 = consume(key)
      assert_consumed(key)
      assert_consumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_unconsumed_copy(self):
    def f(key):
      assert_unconsumed(key)
      consume(key)
      assert_consumed(key)
      key2 = unconsumed_copy(key)
      assert_unconsumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_seed(self):
    def f():
      key = jax.random.key(0)
      assert_unconsumed(key)
    self.check_key_reuse(f)

  def test_split(self):
    def f(key):
      assert_unconsumed(key)
      key2 = jax.random.split(key)
      assert_unconsumed(key2)
      assert_consumed(key)
    self.check_key_reuse(f, jax.random.key(0))

  def test_fold_in(self):
    def f(key):
      assert_unconsumed(key)
      key2 = jax.random.fold_in(key, 2)
      assert_unconsumed(key)
      assert_unconsumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_bits(self):
    def f(key):
      assert_unconsumed(key)
      bits = jax.random.bits(key, (), 'uint32')
      assert_consumed(key)
      return bits
    self.check_key_reuse(f, jax.random.key(0))

  def test_wrap(self):
    def f(key_data):
      key = jax.random.wrap_key_data(key_data)
      assert_unconsumed(key)
    self.check_key_reuse(f, jax.random.PRNGKey(0))

  def test_unwrap(self):
    def f(key):
      assert_unconsumed(key)
      key_data = jax.random.key_data(key)
      assert_consumed(key)
    self.check_key_reuse(f, jax.random.key(0))

  def test_gamma(self):
    def f(key):
      assert_unconsumed(key)
      values = jax.random.gamma(key, 1.0)
      assert_consumed(key)
      return values
    self.check_key_reuse(f, jax.random.key(0))

  def test_broadcast_in_dim(self):
    def f(key):
      assert_unconsumed(key)
      key2 = key[None]
      assert_unconsumed(key)
      assert_unconsumed(key2)
      consume(key)
      assert_consumed(key)
      assert_consumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_copy(self):
    def f(key):
      assert_unconsumed(key)
      key2 = jnp.array(key, copy=True)
      assert_unconsumed(key)
      assert_unconsumed(key2)
      consume(key)
      assert_consumed(key)
      assert_consumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_device_put(self):
    def f(key):
      assert_unconsumed(key)
      key2 = jax.device_put(key)
      assert_unconsumed(key)
      assert_unconsumed(key2)
      consume(key)
      assert_consumed(key)
      assert_consumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_squeeze(self):
    def f(key):
      assert_unconsumed(key)
      key2 = jax.lax.squeeze(key, (0,))
      assert_unconsumed(key)
      assert_unconsumed(key2)
      consume(key)
      assert_consumed(key)
      assert_consumed(key2)
    self.check_key_reuse(f, jax.random.key(0)[None])

  def test_reshape(self):
    def f(key):
      assert_unconsumed(key)
      key2 = key.reshape(1, *key.shape)
      assert_unconsumed(key)
      assert_unconsumed(key2)
      consume(key)
      assert_consumed(key)
      assert_consumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_slice(self):
    def f(keys):
      assert_unconsumed(keys)

      assert_unconsumed(keys[0])
      assert_consumed(keys, np.array([True, False]))

      assert_unconsumed(keys[1])
      assert_consumed(keys, np.array([True, True]))
    self.check_key_reuse(f, jax.random.split(jax.random.key(0)))

  def test_jit_can_consume_input(self):
    def f(key):
      assert_unconsumed(key)
      jax.jit(jax.random.bits)(key)
      assert_consumed(key)
    self.check_key_reuse(f, jax.random.key(0))

  def test_jit_can_return_consumed_output(self):
    def f():
      def g():
        key = jax.random.key(0)
        assert_unconsumed(key)
        bits = jax.random.bits(key)
        assert_consumed(key)
        return bits, key
      _, key = jax.jit(g)()
      assert_consumed(key)
    self.check_key_reuse(f)

  def test_jit_duplicate_inputs(self):
    def f(key):
      assert_unconsumed(key)
      def g(key1, key2):
        assert_unconsumed(key1)
        assert_unconsumed(key2)
        return jax.random.bits(key1)
      _ = jax.jit(g)(key, key)
      assert_consumed(key)
    self.check_key_reuse(f, jax.random.key(0))

  def test_jit_propagates_consumption_bit(self):
    def f(key):
      assert_unconsumed(key)
      g = jax.jit(lambda: key)
      key2 = g()
      assert_unconsumed(key)
      assert_unconsumed(key2)
      consume(key)
      assert_consumed(key)
      assert_consumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_jit_duplicate_outputs(self):
    # TODO(jakevdp): implement this case
    def f(key):
      assert_unconsumed(key)
      def g(key):
        return key, key
      key1, key2 = jax.jit(g)(key)
      assert_unconsumed(key)
      assert_unconsumed(key1)
      assert_unconsumed(key2)
      _ = jax.random.bits(key1)
      assert_consumed(key)
      assert_consumed(key1)
      assert_consumed(key2)
    self.check_key_reuse(f, jax.random.key(0))

  def test_cond_both_consumed(self):
    @jax.jit
    def f(flag, key):
      assert_unconsumed(key)
      _ = jax.lax.cond(
        flag, jax.random.uniform, jax.random.normal, key)
      assert_consumed(key)
    self.check_key_reuse(f, True, jax.random.key(0))

  def test_cond_one_consumed(self):
    @jax.jit
    def f(flag, key):
      assert_unconsumed(key)
      _ = jax.lax.cond(
        flag, jax.random.uniform, lambda k: 1.0, key)
      assert_consumed(key)
    self.check_key_reuse(f, True, jax.random.key(0))

  def test_cond_neither_consumed(self):
    @jax.jit
    def f(flag, key):
      assert_unconsumed(key)
      _ = jax.lax.cond(
        flag, lambda k: 0.0, lambda k: 1.0, key)
      assert_unconsumed(key)
    self.check_key_reuse(f, True, jax.random.key(0))

  def test_simple_vmap(self):
    @jax.jit
    def f(seed):
      key = jax.random.key(seed)
      assert_unconsumed(key)
      result = jax.random.uniform(key)
      assert_consumed(key)
      return result
    self.check_key_reuse(f, 0)
    self.check_key_reuse(jax.vmap(f), jnp.arange(4))

  @parameterized.parameters(*primitives_with_static_signatures)
  def test_jaxpr_type_signature(self, primitive):
    func, *args = primitives_with_static_signatures[primitive]
    signature = _forwarding.key_reuse_signatures[primitive]
    jaxpr = jax.make_jaxpr(func)(*args)
    self.assertEqual(signature, _forwarding.get_jaxpr_type_signature(jaxpr.jaxpr))


@jtu.with_config(jax_enable_key_reuse_checks=False)
class KeyReuseIntegrationTest(jtu.JaxTestCase):
  use_forwarding = True
  random_bits_error = "In random_bits, key values .+ are already consumed.*"
  random_split_error = "In random_split, key values .+ are already consumed.*"
  generic_error = ".*key values .+ are already consumed.*"

  def check_key_reuse(self, f, *args):
    if self.use_forwarding:
      return _forwarding.check_key_reuse(f, *args)
    else:
      return _simple.check_key_reuse(f, *args)

  def test_reuse(self):
    def f():
      key = jax.random.key(0)
      return jax.random.uniform(key) + jax.random.uniform(key)

    with self.assertRaisesRegex(KeyReuseError, self.random_bits_error):
      self.check_key_reuse(f)

  def test_reuse_after_split(self):
    def f_good():
      key = jax.random.key(0)
      key1, key2 = jax.random.split(key)
      return jax.random.uniform(key1) + jax.random.uniform(key2)
    self.check_key_reuse(f_good)

    def f_bad():
      key = jax.random.key(0)
      _ = jax.random.split(key)
      return jax.random.uniform(key)

    with self.assertRaisesRegex(KeyReuseError, self.random_bits_error):
      self.check_key_reuse(f_bad)

    def f_bad_2():
      key = jax.random.key(0)
      _ = jax.random.split(key)
      key1, _ = jax.random.split(key)
      return jax.random.uniform(key1)

    with self.assertRaisesRegex(KeyReuseError, self.random_split_error):
      self.check_key_reuse(f_bad_2)

  def test_repeated_fold_ins(self):
    # TODO(jakevdp): should we allow repeated fold-ins?
    def f():
      key = jax.random.key(0)
      keys = [jax.random.fold_in(key, i)
              for i in range(10)]
      return [jax.random.uniform(k) for k in keys]
    self.check_key_reuse(f)

  def test_reuse_after_fold_in(self):
    def f():
      key = jax.random.key(0)
      _ = jax.random.fold_in(key, 1)
      return jax.random.uniform(key)

    self.check_key_reuse(f)

  def test_reuse_after_broadcast(self):
    def f():
      key = jax.random.key(0)
      key2 = key[None]
      return jax.random.bits(key) + jax.vmap(jax.random.bits)(key2)

    with self.assertRaisesRegex(KeyReuseError, self.random_bits_error):
      self.check_key_reuse(f)

  def test_reuse_after_reshape(self):
    def f():
      key = jax.random.key(0)
      key2 = key.reshape((1,))
      return jax.random.bits(key) + jax.random.bits(key2.squeeze())

    with self.assertRaisesRegex(KeyReuseError, self.random_bits_error):
      self.check_key_reuse(f)

  def test_reuse_after_squeeze(self):
    def f():
      key = jax.random.split(jax.random.key(0), 1)
      key2 = jax.lax.squeeze(key, (0,))
      return jax.random.bits(key.squeeze()) + jax.random.bits(key2)

    with self.assertRaisesRegex(KeyReuseError, self.generic_error):
      self.check_key_reuse(f)

  def test_reuse_after_cond(self):
    def f_good(key, condition):
      return jax.lax.cond(condition, jax.random.uniform, jax.random.normal, key)
    key = jax.random.key(0)
    self.check_key_reuse(f_good, key, True)
    self.check_key_reuse(f_good, key, False)

    # Check where both branches consume the key
    def f_bad(key, condition):
      r1 = jax.lax.cond(condition, jax.random.uniform, jax.random.normal, key)
      return r1 + jax.random.uniform(key)

    with self.assertRaisesRegex(KeyReuseError, self.random_bits_error):
      self.check_key_reuse(f_bad, key, True)

    # Check where only one branch consumes the key
    def f_bad_2(key, condition):
      r1 = jax.lax.cond(condition, jax.random.uniform, lambda key: 1.0, key)
      return r1 + jax.random.uniform(key)

    with self.assertRaisesRegex(KeyReuseError, self.random_bits_error):
      self.check_key_reuse(f_bad_2, key, True)

  def test_simple_scan(self):
    def f_good(key):
      def body_fun(key, _):
        key, subkey = jax.random.split(key)
        return key, jax.random.bits(subkey)
      return jax.lax.scan(body_fun, key, xs=jnp.arange(10))
    self.check_key_reuse(f_good, jax.random.key(0))

  def test_scan_sink_on_consts(self):
    def f(key):
      def body_fun(carry, _):
        return carry, jax.random.uniform(key)
      return jax.lax.scan(body_fun, None, xs=jnp.arange(10))
    with self.assertRaisesRegex(KeyReuseError,  "scan body function leads to key reuse"):
      self.check_key_reuse(f, jax.random.key(0))

  def test_scan_reuse_in_body(self):
    def f_bad(key):
      def body_fun(key, _):
        return key, jax.random.bits(key)
      return jax.lax.scan(body_fun, key, xs=jnp.arange(10))
    with self.assertRaisesRegex(KeyReuseError, "scan body function leads to key reuse"):
      self.check_key_reuse(f_bad, jax.random.key(0))

  def test_scan_good_over_keys(self):
    def f_scan_over_keys(key):
      keys = jax.random.split(key, 5)
      return jax.lax.map(jax.random.bits, keys)
    self.check_key_reuse(f_scan_over_keys, jax.random.key(0))

  def test_vmap(self):
    @jax.vmap
    def f_good(seed):
      key = jax.random.key(seed)
      return jax.random.bits(key)
    self.check_key_reuse(f_good, jnp.arange(4))

    @jax.vmap
    def f_bad(seed):
      key = jax.random.key(0)
      return jax.random.bits(key) + jax.random.bits(key)
    with self.assertRaisesRegex(KeyReuseError, self.random_bits_error):
      self.check_key_reuse(f_bad, jnp.arange(4))

  def test_while_simple(self):
    def f(seed):
      key = jax.random.key(seed)
      def cond_fun(carry):
        return carry[1] < 10
      def body_fun(carry):
        key, subkey = jax.random.split(carry[0])
        return key, carry[1] + jax.random.uniform(subkey)
      return jax.lax.while_loop(cond_fun, body_fun, (key, 0))
    self.check_key_reuse(f, 0)

  def test_while_bad_cond(self):
    def f(seed):
      key = jax.random.key(seed)
      def cond_fun(carry):
        i, key = carry
        return i < jax.random.uniform(key)
      def body_fun(carry):
        i, key = carry
        return i + 1, key
      return jax.lax.while_loop(cond_fun, body_fun, (0, key))
    with self.assertRaisesRegex(KeyReuseError, "while_loop cond"):
      self.check_key_reuse(f, 0)

  def test_while_bad_body(self):
    def f(seed):
      key = jax.random.key(seed)
      def cond_fun(carry):
        key, i = carry
        return i < 5
      def body_fun(carry):
        key, i = carry
        return key, i + jax.random.randint(key, (), 1, 3)
      return jax.lax.while_loop(cond_fun, body_fun, (key, 0))
    with self.assertRaisesRegex(KeyReuseError, "while_loop body function leads to key reuse"):
      self.check_key_reuse(f, 0)

  def test_while_sink_on_body_consts(self):
    def f(seed):
      key = jax.random.key(seed)
      def cond_fun(i):
        return i < 5
      def body_fun(i):
        return i + jax.random.randint(key, (), 1, 3)
      return jax.lax.while_loop(cond_fun, body_fun, 0)
    with self.assertRaisesRegex(KeyReuseError, "while_loop body function leads to key reuse"):
      self.check_key_reuse(f, 0)

  def test_while_sink_on_cond_consts(self):
    def f(seed):
      key = jax.random.key(seed)
      def cond_fun(i):
        return i < jax.random.uniform(key)
      def body_fun(i):
        return i + 1
      return jax.lax.while_loop(cond_fun, body_fun, 0)
    with self.assertRaisesRegex(KeyReuseError, "while_loop cond function leads to key reuse"):
      self.check_key_reuse(f, 0)


class KeyReuseIntegrationTestSimple(KeyReuseIntegrationTest):
  use_forwarding = False


@jtu.with_config(jax_enable_checks=False)
class KeyReuseGlobalFlags(KeyReuseIntegrationTest):
  def test_key_reuse_flag(self):

    @jax.jit
    def f_bad(key):
      return jax.random.bits(key) + jax.random.bits(key)

    @jax.jit
    def f_good(key):
      return jax.random.bits(key)

    key = jax.random.key(0)

    with jax.enable_key_reuse_checks(False):
      f_good(key)
      f_bad(key)  # No failure

    f_bad.clear_cache()
    f_good.clear_cache()

    with jax.enable_key_reuse_checks(True):
      f_good(key)
      with self.assertRaisesRegex(KeyReuseError, "In random_bits.*"):
        f_bad(key)


if __name__ == "__main__":
  absltest.main(testLoader=jtu.JaxTestLoader())
