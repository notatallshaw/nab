# Migrating from split resolvers

nab 0.0.9 merges the two resolvers it used to run into one
engine: the specific (single-environment) resolver and the
universal (matrix) resolver now share the same code path. The
`nab_python.universal` package is deleted, with no compatibility
shims. This guide maps the 0.0.8 surface to the 0.0.9 one.

## What merged

A specific resolve pins one version per package for one marker
environment. A universal resolve covers a declared matrix of
them. See [universal resolution](../explanation/universal.md)
for the model.

Through 0.0.8 each mode had its own entry point, and the matrix
half lived in a separate `nab_python.universal` package. In
0.0.9 the same engine runs both: a project without a matrix
resolves once for the host, a project with a matrix resolves
once per target.

The following are removed in 0.0.9 with no aliases:

* the whole `nab_python.universal` package (`matrix`, `resolve`,
  `provider`, `wheel_selection`, and the rest)
* the `resolve_pyproject`, `resolve_universal_pyproject`, and
  `resolve_universal` entry points
* `ResolutionResult`, `UnsupportedModeError`, `UniversalResult`,
  `TupleResult`, and `MatrixTuple`
* `select_wheel_for_tuple`, `compatible_tags_for_tuple`, and
  `wheel_compatible_with_tuple`
* `build_lock_input_from_provider` and
  `merge_universal_lock_inputs`

`ResolveFork` is not removed but moved: import it from
`nab_python.resolve`.

## Command-line and configuration users

Nothing changes. The same `[tool.nab].mode` and
`[tool.nab.matrix]` tables drive the resolve, and `nab lock`
runs it. A matrix lock is declared and run exactly as it was in
0.0.8. See [Configuration](../reference/configuration.md).

0.0.9 adds two opt-in surfaces, both additive:

* `--python X.Y` resolves for another Python on this machine,
  the way pip's `--python-version` does. It moves only the
  python axis.
* `[tool.nab.environment]` declares one non-host target in
  config. It cannot be combined with `[tool.nab.matrix]`.

Leaving both out keeps the 0.0.8 behaviour.

`[tool.nab.marker-environment]` is deprecated and is translated
to `[tool.nab.environment]` with a warning.

## Python API

The import roots moved. The matrix and target types are in
`nab_python.target`, wheel-tag selection is in `nab_python.tags`,
and both resolve modes share `nab_python.resolve`.

### Single-environment resolve

`resolve_pyproject` becomes `resolve_for_targets`. The call is
the same; the result is a `ResolveResult`, and a
single-environment project produces one target.

Before (0.0.8):

```python
from pathlib import Path

from nab_python.resolve import resolve_pyproject

result = resolve_pyproject(
    Path("pyproject.toml"), transport, python_version="3.12", extras=("cpu",)
)

pins = result.pins
```

After:

```python
from pathlib import Path

from nab_python.resolve import resolve_for_targets

result = resolve_for_targets(
    Path("pyproject.toml"), transport, python_version="3.12", extras=("cpu",)
)

# a single-environment project resolves exactly one target
pins = result.target_results[0].pins
```

### Universal resolve

> [!WARNING]
> The multi-target PEP 751 lockfile a universal resolve produces
> is experimental and may change without notice.

`resolve_universal_pyproject` is gone. The same
`resolve_for_targets` call covers a matrix: the engine reads
`[tool.nab].mode` and `[tool.nab.matrix]` from the config, plans
one target per matrix point, and returns one `ResolveResult`.

Before (0.0.8):

```python
from pathlib import Path

from nab_python.resolve import resolve_universal_pyproject

result = resolve_universal_pyproject(Path("pyproject.toml"), transport=transport)

for tuple_result in result.tuple_results:
    ...
```

After:

```python
from pathlib import Path

from nab_python.resolve import resolve_for_targets

result = resolve_for_targets(Path("pyproject.toml"), transport)

for target_result in result.target_results:
    ...
```

The mode comes from the config, not the function name, so the
call is identical to the single-environment case. Collapse the
per-target pins with `result.merged_pins()`. The low-level entry
point `resolve_with_coordinator` renames its `align_across_tuples`
keyword to `align_across_targets`. It also takes `targets`, a
sequence of `ResolveTarget`, in place of the old `matrix`, and
`requirements` as packaging `Requirement` objects rather than
PEP 508 strings.

### Constructing targets

`Matrix` moves to `nab_python.target` and expands to
`ResolveTarget`, which replaces `MatrixTuple`. `PlatformSpec`
moves to `nab_python.tags`. `Matrix.platforms` takes
`PlatformSpec` values now, not bare id strings, and a target's
marker environment is `ResolveTarget.marker_env`, renamed from
`MatrixTuple.environment`.

Before (0.0.8):

```python
from nab_python.universal.matrix import Matrix
from nab_python.universal.wheel_selection import PlatformSpec

matrix = Matrix(python=">=3.11,<3.14", platforms=("linux_x86_64", "macos_arm64"))

targets = matrix.expand()  # list[MatrixTuple]
env = targets[0].environment
```

After:

```python
from nab_python.target import Matrix, ResolveTarget
from nab_python.tags import PlatformSpec

matrix = Matrix(
    python=">=3.11,<3.14",
    # each platform is a PlatformSpec now, not a bare id string
    platforms=(PlatformSpec("linux_x86_64"), PlatformSpec("macos_arm64")),
)

targets = matrix.expand()  # list[ResolveTarget]
env = targets[0].marker_env

# a single declared target, no matrix
target = ResolveTarget.for_declared(
    python_version="3.12", spec=PlatformSpec("linux_x86_64")
)
```

`MatrixTuple(...)` has no drop-in constructor; build one target
with `ResolveTarget.for_declared`.

### Selecting wheels

The three per-tuple helpers collapse onto `TagSet`. Build the
tag set once with `TagSet.for_spec`, then `pick` a wheel, read
`members` for the compatible tags, and `accepts` a filename.

Before (0.0.8):

```python
from nab_python.universal.wheel_selection import (
    PlatformSpec,
    compatible_tags_for_tuple,
    select_wheel_for_tuple,
    wheel_compatible_with_tuple,
)

spec = PlatformSpec("linux_x86_64")

best = select_wheel_for_tuple(wheels, python_version="3.12", spec=spec)
tags = compatible_tags_for_tuple(python_version="3.12", spec=spec)
ok = wheel_compatible_with_tuple(wheel, python_version="3.12", spec=spec)
```

After:

```python
from nab_python.tags import PlatformSpec, TagSet

tag_set = TagSet.for_spec(
    python_version="3.12", spec=PlatformSpec("linux_x86_64")
)

best = tag_set.pick(wheels)
tags = tag_set.members
ok = tag_set.accepts(wheel.filename)
```

`TagSet.accepts` takes a wheel filename string, where the old
`wheel_compatible_with_tuple` took a wheel object.

### Building the lock input

The per-provider builder `build_lock_input_from_provider`
becomes the per-target `build_target_lock`, imported from
`nab_python.lockfile`. The universal merge step
`merge_universal_lock_inputs` has no separate replacement: the
engine attaches a `TargetLock` to each `TargetResult`, and
`build_lock_input` folds them into one `LockInput`. Both 0.0.8
paths become one call over the finished resolve.

Before (0.0.8):

```python
from nab_python.lockfile import build_lock_input_from_provider
from nab_python.universal.resolve import merge_universal_lock_inputs

# single environment: straight from the provider and pins
lock_input = build_lock_input_from_provider(
    provider, pins, requires_python=">=3.11", extras=("cpu",)
)

# matrix: fold the per-tuple lock inputs into one
lock_input = merge_universal_lock_inputs(
    universal_result, requires_python=">=3.11", extras=("cpu",)
)
```

After:

```python
from nab_python.lockfile import build_target_lock
from nab_python.resolve import build_lock_input

# a manual caller builds one TargetLock per target
target_lock = build_target_lock(provider, target, pins)

# both modes: fold the finished resolve into one LockInput
lock_input = build_lock_input(result, config=config, extras=("cpu",))
```

`requires_python`, the default groups, and the declared
conflicts come from `config` now, not from keyword arguments.

### PlatformSpec fields and the failure model

`PlatformSpec` lives in `nab_python.tags`, and its Linux libc
fields changed. `manylinux_floor` and `musllinux_floor` are
replaced by `libc` (`"glibc"` or `"musl"`) and `libc_version`. A
`free_threaded` field selects the `cpXYt` ABI. The default glibc
floor is 2.28, up from 2.17.

Before (0.0.8):

```python
from nab_python.universal.wheel_selection import PlatformSpec

spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 28))
```

After:

```python
from nab_python.tags import PlatformSpec

spec = PlatformSpec("linux_x86_64", libc="glibc", libc_version=(2, 28))

# free-threaded CPython, the cpXYt ABI
free_threaded = PlatformSpec("linux_x86_64", free_threaded=True)
```

The failure model also changed. `resolve_for_targets` records a
failed target on its `TargetResult` (`success` is False and
`error` holds the `ResolutionError`) instead of raising. Check
`result.success`, or call `result.raise_for_failure()` to
restore the old raise-on-first-failure behaviour.

Before (0.0.8):

```python
from pathlib import Path

from nab_python.resolve import resolve_pyproject

# raised ResolutionError on a failed resolve
result = resolve_pyproject(Path("pyproject.toml"), transport)
```

After:

```python
from pathlib import Path

from nab_python.resolve import resolve_for_targets

result = resolve_for_targets(Path("pyproject.toml"), transport)

# a failed target is recorded, not raised; restore raising with:
result.raise_for_failure()
```
