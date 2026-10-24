# Universal resolution

> [!WARNING]
> Universal mode runs the same resolver as a specific resolve. The multi-target PEP 751 lockfile
> format it produces is experimental and may change without notice.

A specific resolve pins one version per package for one marker environment. A universal resolve
produces a single artifact valid for a set of marker environments.

Declare the supported targets in a [target matrix](../reference/target-matrix.md).
[Lock for multiple environments](../how-to/lock-multiple-environments.md) gives the commands and
output formats.

nab builds targets from the matrix's Python range, platform list and selected implementations
(CPython by default). Dependency markers do not add targets. See
{ref}`how nab chooses coverage <explicit-is-better-than-implicit>`.

## How it works

A matrix expands into a list of resolve targets, one target per `(python, platform, implementation)`
point it names. nab resolves the targets one at a time, each on the same engine and the same
single-environment resolve a project without a matrix runs once for the host.

```text
declared matrix
      |
      v
Python x platform x implementation targets
      |
      v
resolve each target, sharing fetches
      |
      v
merge pins and environment markers
      |
      v
pylock.toml
```

### Shared fetches

The targets share one fetcher, so a package's listing is read once for the whole matrix rather than
once per target. Metadata is shared per wheel rather than per package, so a release publishing one
wheel per interpreter or per platform costs one read for each wheel the matrix picks (see Where a
version's metadata comes from below).

An sdist's `PKG-INFO` stands for the whole version, so one read serves every target that picks it.

### Pin preferences

After a target resolves, its pins flow forward as preferences for the next, giving best-effort
alignment across targets.

nab merges the resolved targets into one lock. It records
[environment declarations](../reference/lockfile-environments.md) and dependency edges for those
targets. When a marker splits a Python minor, nab records one declaration per slice; see
Patch-release markers below.

## Where a version's metadata comes from

nab reads a version's dependency metadata from the one wheel its target's tags rank most preferred
(most specific tag, then highest build tag) and treats it as authoritative for that version on that
target. Per-target tag filtering already keeps cross-platform wheels apart, so this is exact
wherever the installer's own rules can rank a version's wheels.

When a version's wheels tie for a target and the siblings already fetched declare different
dependencies, nab reports an error rather than pick one by guessing. It compares only the siblings
in hand, so it does not promise to catch every such case.

For a dynamic sdist without a static metadata fallback, an explicit
{ref}`assume-universal-deps <assuming-dependencies-are-universal>` override permits reusing a
same-version wheel's dependency declarations across targets.

## Patch-release markers

A matrix names Python minors like 3.11, not exact releases, so a 3.11 target stands for the whole
minor: every micro release from 3.11.0 upward. nab resolves it once, at a representative 3.11.0.

That holds until a dependency marker changes inside the minor. For example,
`some-backport ; python_full_version < "3.11.4"` is needed on 3.11.3 but not 3.11.5.

nab splits the 3.11 target at 3.11.4 and resolves each side. Each slice gets its own pins and
`environments` row.

### Split a minor at a marker boundary

Take a project that targets just 3.11 on one platform:

```toml
[project]
name = "example-lib"
version = "0.1.0"
requires-python = ">=3.11,<3.12"
dependencies = [
    'some-backport ; python_full_version < "3.11.4"',
]

[tool.nab]
mode = "universal"

[tool.nab.matrix]
python = ">=3.11,<3.12"
platforms = ["linux_x86_64"]
```

The marker cuts the minor at 3.11.4, so the lock declares two environments where an unsplit minor
would declare one:

```toml
environments = [
    'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and python_full_version < "3.11.4"',
    'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and python_full_version >= "3.11.4.dev0"',
]
```

The two rows are identical but for the last clause. `some-backport` is pinned only on the lower
slice, so its marker names that slice and it is absent from the other. The rows agree on everything
but `python_full_version`, and a per-package marker only has to be right inside the declared
environments, so that is all the marker says:

```toml
[[packages]]
name = "some-backport"
version = "1.2.0"
marker = 'python_full_version < "3.11.4"'
```

### Prerelease boundaries

The slices meet at `3.11.4.dev0`, not `3.11.4`. A plain `>= "3.11.4"` upper edge would leave
prereleases such as `3.11.4rc1` in neither slice: PEP 440 also excludes them from `< "3.11.4"`.

Starting the upper slice at `3.11.4.dev0` closes that gap and gives a 3.11.4 prerelease the pins
intended for 3.11.4.

### Repeated splits and unsupported markers

A split can pull in a dependency the whole minor did not, and that dependency's marker can name a
fresh boundary, so nab re-splits a target until a pass finds no new one.

Every comparison that names an interval cuts a minor: the ordered operators (`<`, `<=`, `>`, `>=`)
and the ones naming a region (`==`, `!=`, `~=`, `== V.*`).

On a minor-interval target, nab rejects a `python_full_version` marker it cannot turn into an
interval: a membership test (`in`, `not in`), a verbatim `===`, a non-version comparison, a
comparison against another marker variable, or certain pre- or post-release literals strictly inside
the minor.

### Pin one patch release

To resolve a minor as one real release rather than split it, set the patch you deploy on:

```toml
[tool.nab.matrix.python-patches]
"3.11" = "3.11.4"
"3.12" = "3.12.1"
```

A minor a patch names is not split: it sits on that single release and resolves whole, like the host
interpreter.

## Interpreter implementations

`implementations` selects the interpreter implementations to model. It defaults to `["cpython"]`, so
leaving it out keeps the matrix and its lockfile output unchanged.

```toml
[tool.nab.matrix]
python = ">=3.11,<3.14"
platforms = ["linux_x86_64"]
implementations = ["cpython", "pypy"]
```

By default, each implementation uses the whole Python range. To target CPython 3.10, 3.11, 3.12,
3.13, 3.14 and 3.15 and only PyPy 3.11 on the same platforms, narrow PyPy's range:

```toml
[tool.nab.matrix]
python = ">=3.10,<3.16"
platforms = ["linux_x86_64", "macos_arm64"]
implementations = ["cpython", "pypy"]

[tool.nab.matrix.overrides.implementations.pypy]
python = "==3.11"
```

### Per-implementation overrides

This declares 14 targets: six CPython minors and one PyPy minor on each platform. The keyed
`overrides.implementations` table replaces `python` or `platforms` for one interpreter; omitted
fields inherit the shared matrix values. For example, adding `platforms = ["linux_x86_64"]` to the
PyPy table restricts PyPy to Linux while CPython keeps both platforms. An override may also add
Python minors or platform IDs outside the shared defaults.

`implementations` still selects which interpreters to include. `python-order` orders all selected
minors together. Declare implementation overrides in the project file; the shared-axis CLI flags
change only inherited values. `--project-matrix-implementations cpython` selects only CPython for
one run, leaving the PyPy override inactive.

### PyPy markers and wheel tags

A PyPy target sets `platform_python_implementation = "PyPy"` / `implementation_name = "pypy"` for
marker evaluation and accepts `ppXY-pypyXY_ppNN` wheel tags instead of `cpXY`. Labels use the `pp`
interpreter prefix (`pp311-linux_x86_64`).

A matrix containing PyPy records `implementation_name` in its target environment declarations. A
CPython-only matrix also records it when a consulted marker names it. Package markers distinguish
implementations when their selections differ; a PyPy-only lock refuses CPython.

PyPy's `implementation_version` is modelled as its Python level rather than its own release. A
marker comparing that value with a PyPy release can misevaluate during resolution. The lock leaves
the synthetic value out, so the interpreter accepts the lock, but a dependency selected by it may be
missed.

## Resolution axes

The `[tool.nab.matrix]` keys drive two decisions per target: how each PEP 508 marker evaluates and
which wheels the target can install.

### Marker variables

The
[dependency specifiers specification](https://packaging.python.org/en/latest/specifications/dependency-specifiers/#environment-markers)
defines these variables. The table below maps them to Nab's target settings.

Every PEP 508 environment variable gets a value in every target, so no marker ever evaluates against
a missing key. The table shows each target's effective axes, including any
`overrides.implementations.<name>` overrides, and fixed defaults.

| Marker variable                  | Set by                           | Default                                                                   |
| -------------------------------- | -------------------------------- | ------------------------------------------------------------------------- |
| `python_version`                 | `python`                         | one value per minor in range                                              |
| `python_full_version`            | `python`, `python-patches`       | `<minor>.0`                                                               |
| `implementation_version`         | `python`, `python-patches`       | same as `python_full_version`                                             |
| `implementation_name`            | `implementations`                | `cpython`                                                                 |
| `platform_python_implementation` | `implementations`                | `CPython`                                                                 |
| `sys_platform`                   | `platforms`                      | per id (`linux`, `darwin`, `win32`)                                       |
| `platform_system`                | `platforms`                      | per id (`Linux`, `Darwin`, `Windows`)                                     |
| `platform_machine`               | `platforms`                      | per id (`x86_64`, `aarch64`, `i686`, `armv7l`, `arm64`, `AMD64`, `ARM64`) |
| `os_name`                        | `platforms`                      | per id (`posix`, `nt`)                                                    |
| `platform_release`               | `platforms` (`platform-release`) | `""`                                                                      |
| `platform_version`               | `platforms` (`platform-version`) | `""`                                                                      |

`extra`, `extras`, and `dependency_groups` are not axes. `extra` is bound while a version's
dependencies are sorted into the base package and its extras, so `extra == "cpu"` names the
dependencies of `pkg[cpu]`.

The other two are empty during resolution. nab uses their clauses only in the lockfile marker that a
consumer evaluates.

### How the axes couple

One axis usually sets several variables at once, so an impossible combination cannot be declared:

- `platforms` sets `sys_platform`, `platform_system`, `platform_machine`, and `os_name` together per
  id. You pick `linux_x86_64`, not the four separately, so a Linux `sys_platform` can never pair
  with a macOS `platform_machine`.
- `implementations` sets `implementation_name` and `platform_python_implementation` together.
- `python` sets `python_version`, `python_full_version`, and `implementation_version` together.

### Wheel selection

The matrix also decides which wheels a tuple can install, computed from the python version,
platform, and implementation without a live interpreter. A version whose only wheels are
tag-incompatible with a tuple is dropped for that tuple; a version that also ships a `.tar.gz` sdist
stays, subject to the build policy. Legacy `.zip` sdists require `allow-zip-sdists = true` and
follow the same metadata rules. Each tuple accepts three wheel-tag dimensions:

- interpreter: `cpXY` for CPython, `ppXY` for PyPy, plus the interpreter-agnostic `py3` tags.
- abi: `cpXY` and `abi3` for CPython, `cpXYt` and `abi3t` on a free-threaded target, `pypyXY_ppNN`
  for PyPy, and `none`.
- platform: manylinux or musllinux for the declared libc family, macosx, and win.

Wheel compatibility settings go in a platform table within `platforms`, alongside its `id`.
[Platform tags](../reference/platform-tags.md) lists the defaults and constraints.

### What the axes do not cover

The matrix and platform settings define the supported targets. Platform IDs and implementations are
fixed enumerations, so unknown names are configuration errors.

`python` accepts a specifier over known minor versions. A range can extend beyond those versions;
only a range matching none of them is an error.

`platform_release` and `platform_version` name one machine's kernel build. Both default to the empty
string, so a marker comparing the kernel (`platform_release >= "5.10"`) evaluates False and its
dependency is dropped: a target that does run that kernel has to declare it.
