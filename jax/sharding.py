# Copyright 2022 The JAX Authors.
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

# Note: import <name> as <name> is required for names to be exported.
# See PEP 484 & https://github.com/jax-ml/jax/issues/7570

from jax._src.sharding import Sharding as Sharding
from jax._src.sharding_impls import (
    NamedSharding as NamedSharding,
    SingleDeviceSharding as SingleDeviceSharding,
    PmapSharding as PmapSharding,
    set_mesh as set_mesh,
)
from jax._src.partition_spec import (
    PartitionSpec as PartitionSpec,
)
from jax._src.mesh import (
    Mesh as Mesh,
    AbstractMesh as AbstractMesh,
    AxisType as AxisType,
    get_abstract_mesh as get_abstract_mesh,
)

from jax._src.pjit import (
    reshard as reshard,
    auto_axes as auto_axes,
    explicit_axes as explicit_axes,
)
