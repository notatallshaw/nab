# Target matrix

Resolve CPython 3.10 through 3.15 on Linux and macOS into one lock:

```toml
[tool.nab.matrix]
python = ">=3.10,<3.16"
platforms = ["linux_x86_64", "macos_arm64"]
```

Nab resolves every Python/platform combination. CPython is the default implementation; targets must
satisfy the project's `requires-python`. The multi-target lockfile format is experimental. For
commands and output formats, see
[Lock for multiple environments](../how-to/lock-multiple-environments.md).

(matrix-include)=

## Add target groups with `include`

Prefer `include` for additional combinations, such as PyPy 3.11 on Windows:

```toml
[tool.nab.matrix]
python = ">=3.10,<3.16"
platforms = ["linux_x86_64", "macos_arm64"]

# Add a group outside the base Python/platform combinations.
[[tool.nab.matrix.include]]
python = "==3.11"
platforms = ["windows_amd64"]
implementations = ["pypy"]
```

Each entry inherits omitted settings and replaces supplied values in full. Entries expand
independently; they do not modify the base or one another. Exact duplicate targets merge, while
different platform settings remain distinct.

(matrix-free-threaded)=

## Add free-threaded builds

Keep non-free-threaded CPython 3.10 through 3.15, and add free-threaded 3.15:

```toml
[tool.nab.matrix]
python = ">=3.10,<3.16"
platforms = ["linux_x86_64", "macos_arm64", "windows_amd64"]

# Inherit platforms and CPython; narrow the added builds to 3.15.
[[tool.nab.matrix.include]]
python = "==3.15"
free-threaded = true
```

For free-threaded builds only, set `free-threaded = true` in the base matrix. They require CPython
3.13 or newer. The flag selects the compiled ABI, regardless of the runtime GIL setting, and
replaces any platform-level flag. Explicit `false` selects the non-free-threaded ABI.

Both builds must share package selections and pins: PEP 508 has no threading marker. Nab rejects
incompatible selections instead of writing an ambiguous lock. Installers choose the compatible
wheel. The former `python-builds` list and CLI flag are replaced by `free-threaded` and `include`.

## Specialize selected implementations with `overrides`

Use an override when the specialization should follow CLI implementation selection:

```toml
[tool.nab.matrix]
python = ">=3.10,<3.16"
platforms = ["linux_x86_64", "macos_arm64"]
implementations = ["cpython", "pypy"]

# Apply only while PyPy is selected.
[tool.nab.matrix.overrides.implementations.pypy]
python = "==3.11"
platforms = ["linux_x86_64"]
```

```bash
# Resolve CPython on both platforms; leave the PyPy override inactive.
nab lock --project-matrix-implementations cpython

# Resolve only PyPy 3.11 on Linux.
nab lock --project-matrix-implementations pypy
```

An override can replace `python`, `platforms` or `free-threaded`; it does not enable an
implementation. Inactive overrides are still validated. An explicit PyPy `include`, by contrast,
remains active when the CLI narrows the base implementations to CPython.

When both apply, precedence is shared defaults, implementation override, then explicit include
values. Declare overrides and includes in `pyproject.toml` or the project-directory `nab.toml`.

## Order targets and pin patch releases

Resolve newer Python minors first, and resolve 3.11 as exactly 3.11.4:

```toml
[tool.nab.matrix]
python = ">=3.11,<3.14"
platforms = ["linux_x86_64"]
python-order = "desc"
python-patches = { "3.11" = "3.11.4" }
```

Successful pins become preferences for later targets. Order and patch pins apply to all entries.
Within a minor, base targets precede includes in declaration order. Groups retain platform and
implementation order; overrides retain shared platform-ID order before adding new IDs. Without a
patch pin, Nab can [split a minor at dependency-marker boundaries](../explanation/universal.md).

## Set shared defaults for one run

```bash
nab lock \
  --project-matrix-python '>=3.11,<3.14' \
  --project-matrix-platforms windows_amd64 linux_x86_64 libc=musl runs-on-libc=1.2 \
  --project-matrix-python-patches 3.11=3.11.4
```

Each flag replaces one shared key. Overrides and explicit include values retain precedence. Without
a file matrix, the Python and platform flags are required. `--project-matrix-free-threaded` sets the
base flag to true; its `--no-` form sets it to false.

## Choose a mode

```bash
# Resolve the host environment instead of a file's matrix.
nab lock --project-mode specific
```

With no explicit mode, one distinct matrix target selects `specific`; multiple targets select
`universal`. `mode = "universal"` also permits a singleton. `mode = "specific"` rejects multiple
targets at the same precedence; a higher-precedence CLI flag suppresses the file matrix.

## Settings

| Key               | Default           | Meaning                                                                             |
| ----------------- | ----------------- | ----------------------------------------------------------------------------------- |
| `python`          | required          | PEP 440 range over known Python minors                                              |
| `platforms`       | required          | Platform ids or [compatibility tables](platform-tags.md)                            |
| `implementations` | `["cpython"]`     | `"cpython"`, `"pypy"`, or both                                                      |
| `free-threaded`   | platform settings | Select the free-threaded ABI with `true`, or the non-free-threaded ABI with `false` |
| `include`         | `[]`              | Additional groups inheriting omitted settings                                       |
| `overrides`       | none              | Conditional replacements under `implementations.<name>`                             |
| `python-order`    | `"asc"`           | Older minors first; `"desc"` means newer first                                      |
| `python-patches`  | none              | Map a minor to one full Python version                                              |

Ranges select language minors: use `==3.11`, not `==3.11.4`. Put patch pins in `python-patches`.
Full Python versions accept up to three release components and prerelease, development and local
suffixes; nonzero epochs and post releases are rejected. Empty effective ranges, unknown platforms
or implementations, and unsupported interpreter/ABI combinations are errors.
