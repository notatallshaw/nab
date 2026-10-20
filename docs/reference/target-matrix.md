# Target matrix

For a complete workflow, see
[Lock for multiple environments](../how-to/lock-multiple-environments.md).

The matrix declares `(python, platform, implementation)` targets. nab resolves each target with the
same engine. Targets share one fetcher: one listing per package, metadata once per selected wheel,
and PKG-INFO once per sdist version. The multi-target lockfile format is experimental.

```toml
[tool.nab.matrix]
python = ">=3.11,<3.14"
platforms = ["linux_x86_64", "macos_arm64"]
implementations = ["cpython", "pypy"]   # default ["cpython"]
python-order = "asc"                    # "asc" | "desc"
python-patches = { "3.11" = "3.11.4" }
```

The matrix has three axes. Without implementation overrides, nab resolves the full cross product:
the example above declares 3 pythons, 2 platforms and 2 implementations, so it plans 12 targets.

## Targets nab includes

nab requires `python` and `platforms` and resolves their listed combinations. `implementations`
defaults to CPython only; nab includes PyPy only when you list `"pypy"`. nab uses the documented
defaults for omitted platform settings; it does not expand them into extra targets. See
{ref}`how nab chooses coverage <explicit-is-better-than-implicit>`.

| Key               | Default       | Meaning                                                                                    |
| ----------------- | ------------- | ------------------------------------------------------------------------------------------ |
| `python`          | required      | A PEP 440 range like `>=3.11,<3.14`, expanded into one target per minor                    |
| `platforms`       | required      | The platforms to model; a bare id, or a table of [platform tag settings](platform-tags.md) |
| `implementations` | `["cpython"]` | The interpreter implementations to model: `"cpython"`, `"pypy"`                            |
| `overrides`       | none          | Keyed bodies under `implementations.<name>`, with optional `python` and `platforms`        |
| `python-order`    | `"asc"`       | Cross-target alignment direction                                                           |
| `python-patches`  | none          | Pins a minor to one patch release, resolved whole instead of split into slices             |

## Mode selection

With `mode` omitted, nab uses `specific` for one target and `universal` for multiple targets. It
counts the targets after expanding Python minors and applying overrides for enabled implementations.
An override for a disabled implementation does not affect the inferred mode.

A singleton matrix selects its declared environment. Explicit `mode = "universal"` forces universal
behavior even with one target. Explicit `specific` rejects multiple targets at the same precedence;
a higher-precedence `--project-mode specific` suppresses a file matrix for that run.

## Resolution order

`python-order` resolves selected Python minors oldest first (`asc`, the default) or newest first
(`desc`). Successful pins become preferences for later targets when compatible.

## Interpreter implementations

`implementations` names the interpreters to model. An unknown implementation, a duplicate, and an
empty list are each a config error. See [Universal resolution](../explanation/universal.md) for how
the axis is modelled and what it puts on the lockfile markers.

`[tool.nab.matrix.overrides.implementations.<name>]` replaces `python` or `platforms` for one
implementation. Omitted fields inherit the shared matrix values. For CPython 3.10 through 3.15 on
Linux and Apple Silicon macOS, and PyPy 3.11 on Windows and musl Linux:

```toml
[tool.nab.matrix]
python = ">=3.10,<3.16"
platforms = ["linux_x86_64", "macos_arm64"]
implementations = ["cpython", "pypy"]

[tool.nab.matrix.overrides.implementations.pypy]
python = "==3.11"
platforms = [
    "windows_amd64",
    { id = "linux_x86_64", libc = "musl", runs-on-libc = "1.2" },
]
```

An override uses the same minor-version specifiers and platform string/table forms as the shared
axes. Its explicit values replace the corresponding defaults, so they may add Python minors or
platform IDs outside those defaults. Every selected Python minor must still satisfy the project's
`requires-python`. An empty effective Python range or platform list is a config error.

### Inherited and disabled overrides

Keys must name known implementations. An override does not enable its implementation, and overrides
for disabled implementations are allowed. `python-order` orders selected minors across all
implementations; `python-patches` pins every target of that minor to the same patch release. Shared
platform IDs keep their matrix order, followed by new IDs in enabled-implementation order.

### Accepted Python versions

Python targets accept up to three release components with optional prerelease, development and local
suffixes. Nonzero epochs and post releases are rejected in `environment.python`,
`marker-environment` Python values and `matrix.python-patches`.

## Declaring the matrix on the command line

```bash
nab lock \
  --project-matrix-python '>=3.11,<3.14' \
  --project-matrix-platforms windows_amd64 linux_x86_64 libc=musl runs-on-libc=1.2 \
  --project-matrix-python-patches 3.11=3.11.4
```

`--project-matrix-platforms` takes platform ids and `KEY=VALUE` tag settings up to the next flag.
`true` and `false` are booleans. `--project-matrix-python-patches` takes `MINOR=FULL` tokens, while
`--project-matrix-implementations` takes names.

Each flag replaces one table key and leaves the others in place:

```bash
nab lock --project-matrix-platforms macos_arm64
```

The Python and platform flags set the shared defaults. An explicit value in
`[tool.nab.matrix.overrides.implementations.<name>]` keeps precedence over its shared default.
Declare these keyed override bodies in `pyproject.toml` or the project-directory `nab.toml`; they
have no CLI flags. To select only CPython from a file declaring both implementations:

```bash
nab lock --project-matrix-implementations cpython
```

The PyPy override remains configured but is inactive for that run.

Without a file matrix, `--project-matrix-python` and `--project-matrix-platforms` are required.
Other keys use their defaults.
