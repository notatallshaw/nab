# Target matrix

For a complete workflow, see
[Lock for multiple environments](../how-to/lock-multiple-environments.md).

The matrix declares Python versions, platforms, implementations and build features. Nab resolves
only the declared targets and reuses index listings and metadata across them. The multi-target
lockfile format is experimental.

```toml
[tool.nab.matrix]
python = ">=3.11,<3.14"
platforms = ["linux_x86_64", "macos_arm64"]
implementations = ["cpython", "pypy"]
python-order = "asc"
python-patches = { "3.11" = "3.11.4" }
```

The base entry resolves the cross product of its Python versions, platforms and implementations.

## Shared defaults

`python` and `platforms` are required. `implementations` defaults to `["cpython"]`. Nab uses the
documented defaults for omitted platform settings; it does not infer additional targets. See
{ref}`how Nab chooses coverage <explicit-is-better-than-implicit>`.

| Key               | Default           | Meaning                                                                                 |
| ----------------- | ----------------- | --------------------------------------------------------------------------------------- |
| `python`          | required          | A PEP 440 range, expanded into one target per known minor                               |
| `platforms`       | required          | Platform ids or tables of [wheel compatibility settings](platform-tags.md)              |
| `implementations` | `["cpython"]`     | Interpreter implementations: `"cpython"` or `"pypy"`                                    |
| `free-threaded`   | platform settings | Select the free-threaded ABI with `true`, or the non-free-threaded ABI with `false`     |
| `overrides`       | none              | Specialize enabled implementations; prefer include entries for additional target groups |
| `include`         | `[]`              | Additional target groups inheriting omitted axes from this base entry                   |
| `python-order`    | `"asc"`           | Resolve older minors first; `"desc"` resolves newer minors first                        |
| `python-patches`  | none              | Pin a minor to one patch release instead of resolving its micro-version slices          |

(matrix-include)=

## Additional target groups

Prefer `include` for new configurations that declare additional target combinations. Each
`[[tool.nab.matrix.include]]` table adds a target group. It may replace `python`, `platforms`,
`implementations` or `free-threaded`. Omitted values inherit the shared defaults, specialized by any
override for the selected implementation. An entry's explicit values take precedence over those
defaults. Entries do not inherit from one another or modify the base entry.

For CPython 3.10 through 3.15 and PyPy 3.11 on Windows:

```toml
[tool.nab.matrix]
python = ">=3.10,<3.16"
platforms = ["linux_x86_64", "macos_arm64"]

# Add PyPy 3.11 on Windows.
[[tool.nab.matrix.include]]
python = "==3.11"
platforms = ["windows_amd64"]
implementations = ["pypy"]
```

An explicit range or platform list replaces the whole inherited value, so an entry can declare
versions or platforms outside the base entry. Every resulting Python target must still satisfy the
project's `requires-python`.

Overlapping entries emit each exact target once. Different platform settings remain distinct
targets, even when their platform id is the same. Each entry must independently describe a supported
interpreter/ABI combination; unsupported combinations are errors.

Global `python-order` and `python-patches` apply to every entry. Within one Python minor, the base
targets precede additional entries in declaration order. Each group retains its platform and
implementation ordering.

(matrix-free-threaded)=

## Free-threaded builds

Use an include entry to add free-threaded CPython only for the versions you support:

```toml
[tool.nab.matrix]
python = ">=3.10,<3.16"
platforms = ["linux_x86_64", "macos_arm64", "windows_amd64"]

# Add the free-threaded ABI only for Python 3.15.
[[tool.nab.matrix.include]]
python = ">=3.15,<3.16"
free-threaded = true
```

This selects the non-free-threaded CPython builds across the base range, plus free-threaded CPython
3.15. To select only free-threaded builds, put `free-threaded = true` in the base entry and use a
Python range starting at 3.13 or later. A free-threaded entry must select CPython.

`free-threaded` describes the compiled ABI, not whether the GIL is enabled at runtime. It overrides
the entry's platform build settings when supplied; otherwise their settings apply. An include entry
with `free-threaded = false` can select the non-free-threaded ABI even when the base flag is true.

Both builds resolve their own wheel tags and metadata. Matching pins merge, and pip selects the
compatible wheel. Because PEP 508 has no threading marker, entries sharing an environment must agree
on package selections and pins; Nab refuses a shared lock when they cannot.

The named `python-builds` list has been replaced by explicit feature flags and include entries.

## Conditional implementation overrides

`overrides.implementations` specializes implementations selected by the base matrix or an include
entry. A body may replace `python`, `platforms` or `free-threaded`; omitted values keep the shared
defaults. Declaring an override does not itself enable an implementation.

```toml
[tool.nab.matrix]
python = ">=3.10,<3.16"
platforms = ["linux_x86_64", "macos_arm64"]
implementations = ["cpython", "pypy"]

[tool.nab.matrix.overrides.implementations.pypy]
python = "==3.11"
platforms = ["linux_x86_64"]
```

This preserves the CLI selection workflow: `--project-matrix-implementations cpython` selects only
the base CPython targets and leaves the PyPy override inactive. Selecting PyPy applies its declared
Python and platform replacements. Dormant bodies are still validated.

Use `include` for additional, independently declared target groups. Keep an override when its
specialization should follow the implementations selected for a run. An explicit PyPy include
remains active when a CLI flag narrows only the base defaults, so converting an override to an
include is not behavior-preserving for every workflow.

When both mechanisms apply, the order is shared defaults, implementation override, then explicit
include fields. An include that selects PyPy can inherit its override; explicit Python, platform or
feature values in that entry take precedence.

## Mode selection

With `mode` omitted, Nab uses `specific` for one distinct target and `universal` for multiple
targets. The count includes all entries after deduplication.

A singleton matrix selects its declared environment. Explicit `mode = "universal"` forces universal
behavior even with one target. Explicit `specific` rejects multiple targets at the same precedence;
a higher-precedence `--project-mode specific` suppresses the file matrix for that run.

## Declaring shared defaults on the command line

```bash
nab lock \
  --project-matrix-python '>=3.11,<3.14' \
  --project-matrix-platforms windows_amd64 linux_x86_64 libc=musl runs-on-libc=1.2 \
  --project-matrix-python-patches 3.11=3.11.4
```

Each flag replaces one shared key. Explicit values in include entries retain precedence over
inherited values. Declare the entries in `pyproject.toml` or the project-directory `nab.toml`.

`--project-matrix-free-threaded` sets the base flag to true; `--no-project-matrix-free-threaded`
sets it to false. `--project-matrix-implementations` takes interpreter names, and
`--project-matrix-python-patches` takes `MINOR=FULL` tokens.

Without a file matrix, `--project-matrix-python` and `--project-matrix-platforms` are required.
Other shared keys use their defaults.

## Accepted Python versions

Python target values accept up to three release components with optional prerelease, development and
local suffixes. Nonzero epochs and post releases are rejected in `environment.python`,
`marker-environment` Python values and `matrix.python-patches`. Matrix ranges, including ranges in
include entries, select language minors; patch pins belong in `python-patches`.
