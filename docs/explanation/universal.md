# Universal resolution

> [!WARNING]
> Universal mode runs the same resolver as a specific resolve. The
> multi-target PEP 751 lockfile format it produces is experimental
> and may change without notice.

A specific resolve pins one version per package for one marker
environment. A universal resolve produces a single artefact
valid for a set of marker environments.

nab's universal-resolution model is user-driven: the user
declares their target Python range and platform list in
`[tool.nab.matrix]`, and the resolver only ever considers what
they declared.

## How it works

A matrix expands into a list of resolve targets, one target per
`(python, platform, implementation)` point it names. nab resolves the
targets one at a time, each on the same engine and the same
single-environment resolve a project without a matrix runs once for the
host.

The targets share one fetcher, so a package's listing is read once for
the whole matrix rather than once per target. Metadata is shared per
wheel rather than per package, so a release publishing one wheel per
interpreter or per platform costs one read for each wheel the matrix
picks (see Where a version's metadata comes from below). An sdist's
`PKG-INFO` stands for the whole version, so one read serves every
target that picks it.

After a target resolves, its pins flow forward as preferences for the
next, giving best-effort alignment across targets.

The lock a matrix produces is the lock a single environment produces,
with more environments in it: the same shape, the same
[environment declarations](../reference/lockfile.md), and the same
dependency edges. A target whose minor a marker splits contributes
one declaration per slice (see Patch-release markers below).

## Where a version's metadata comes from

nab reads a version's dependency metadata from the one wheel its
target's tags rank most preferred (most specific tag, then highest
build tag) and treats it as authoritative for that version on that
target. Per-target tag filtering already keeps cross-platform wheels
apart, so this is exact wherever the installer's own rules can rank a
version's wheels.

When a version's wheels tie for a target and the siblings already
fetched declare different dependencies, nab reports an error rather
than pick one by guessing. It compares only the siblings in hand, so
it does not promise to catch every such case.

## Declaring the matrix

```toml
[project]
name = "example-lib"
version = "0.1.0"
requires-python = ">=3.11,<3.14"
dependencies = [
    "numpy",
    "fastapi",
]

[tool.nab]
mode = "universal"

[tool.nab.matrix]
python = ">=3.11,<3.14"
platforms = ["linux_x86_64", "macos_arm64"]
python-order = "asc"
```

`mode` selects the resolve. It defaults to `"specific"`, one version
per package for a single environment. `"universal"` resolves every
target the matrix declares, and a project file setting one without the
other is a config error.

`python` is a PEP 440 specifier expanded into one target per
minor version. `platforms` is a list of platform ids
(`linux_x86_64`, `linux_aarch64`, `linux_i686`, `linux_armv7l`,
`macos_x86_64`, `macos_arm64`, `windows_amd64`, `windows_arm64`), each
optionally written as a table to declare its
wheel-tag knobs (libc family, the libc and macOS the lock must run
on, free-threaded build).  See
[Configuration](../reference/configuration.md).

`python-order` selects the resolution direction:

* `"asc"` (default): oldest Python first. Pins propagate forward
  as preferences; the lowest common version usually wins.
  Mirrors uv's `fork-strategy=fewest`.
* `"desc"`: newest Python first. Pins propagate backward; older
  Pythons diverge only when the new pin is incompatible.
  Mirrors uv's `fork-strategy=requires-python`.

### On the command line

The same matrix, on a project that declares none:

```bash
nab lock --project-mode universal \
  --project-matrix-python '>=3.11,<3.14' \
  --project-matrix-platforms linux_x86_64 macos_arm64
```

The project declares no matrix here, so there is nothing to narrow:
`--project-matrix-python` and `--project-matrix-platforms` are both
required and the other three take their defaults. The flags declare the
matrix and not the mode, so `--project-mode universal` goes with them.

A project that declares a matrix narrows one key with one flag, and its
file already sets the mode:

```bash
nab lock --project-matrix-platforms macos_arm64
```

## Run

```bash
nab lock pyproject.toml
```

Writes a single PEP 751 `pylock.toml` covering the whole matrix.
Packages whose pinned version differs across targets appear as
multiple `Package` entries with PEP 508 markers; packages that
agree across every target appear once with no marker.

## Inspect the per-target pins

```bash
nab lock --format requirements-without-hashes --output - pyproject.toml
```

```
warning: the multi-target ('universal') lockfile format is experimental and may change without notice
# py311-linux_x86_64
fastapi==0.115.2
numpy==2.0.2
...
# py311-macos_arm64
fastapi==0.115.2
numpy==2.0.2
...
# py312-linux_x86_64
fastapi==0.115.2
numpy==2.1.3
...
```

One block per target. Pip cannot install a single requirements.txt
across multiple targets in hash-checking mode, so the per-target
block format is for inspection or for tools that consume one block
at a time. When a target fails resolution, the line reads
`# <label>: FAILED` followed by the indented error and the process
exits 1.

## Patch-release markers

A matrix names Python minors like 3.11, not exact releases, so a 3.11
target stands for the whole minor: every micro release from 3.11.0
upward. nab resolves it once, at a representative 3.11.0.

That holds until a dependency's marker turns on a patch release inside
the minor. If the project asks for
`some-backport ; python_full_version < "3.11.4"`, then 3.11.3 needs the
backport and 3.11.5 does not, yet a single resolve at 3.11.0 would
answer the marker one way for the whole minor. So nab splits the 3.11
target at 3.11.4 and resolves each side on its own. Each side is a slice
of the target, with its own pins and its own `environments` row.

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

The marker cuts the minor at 3.11.4, so the lock declares two
environments where an unsplit minor would declare one:

```toml
environments = [
    'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and python_full_version < "3.11.4"',
    'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and python_full_version >= "3.11.4.dev0"',
]
```

The two rows are identical but for the last clause. `some-backport` is
pinned only on the lower slice, so its marker names that slice and it is
absent from the other. The rows agree on everything but
`python_full_version`, and a per-package marker only has to be right
inside the declared environments, so that is all the marker says:

```toml
[[packages]]
name = "some-backport"
version = "1.2.0"
marker = 'python_full_version < "3.11.4"'
```

The two slices meet at `3.11.4.dev0`, not at `3.11.4`. The lower slice
ends at `< "3.11.4"` and the upper starts at `>= "3.11.4.dev0"`. A plain
`>= "3.11.4"` upper edge would strand the prereleases of 3.11.4, such as
`3.11.4rc1`: PEP 440 keeps them below `>= "3.11.4"`, and `< "3.11.4"`
excludes them too, so they would land in neither slice. Snapping the
edge down to `3.11.4.dev0` puts them on the upper side, so the slices
meet exactly, with no gap and no overlap, and a user on `3.11.4rc1` gets
the pins meant for 3.11.4.

A split can pull in a dependency the whole minor did not, and that
dependency's marker can name a fresh boundary, so nab re-splits a target
until a pass finds no new one.

Every comparison that names an interval cuts a minor: the ordered
operators (`<`, `<=`, `>`, `>=`) and the ones naming a region (`==`,
`!=`, `~=`, `== V.*`). A `python_full_version` marker nab cannot turn
into an interval is a loud error rather than a silent guess: a
membership test (`in`, `not in`), a verbatim `===`, a non-version
comparison, a comparison against another marker variable, or certain
pre- or post-release literals strictly inside the minor.

To resolve a minor as one real release rather than split it, name the
patch you deploy on:

```toml
[tool.nab.matrix.python-patches]
"3.11" = "3.11.4"
"3.12" = "3.12.1"
```

A minor a patch names is not split: it sits on that single release and
resolves whole, like the host interpreter.

## Interpreter implementations

`implementations` selects the interpreter implementations to model.
It defaults to `["cpython"]`, so leaving it out keeps the matrix and
its lockfile output unchanged.

```toml
[tool.nab.matrix]
python = ">=3.11,<3.14"
platforms = ["linux_x86_64"]
implementations = ["cpython", "pypy"]
```

Each implementation multiplies the target count (pythons x platforms x
implementations). A PyPy target sets `platform_python_implementation =
"PyPy"` / `implementation_name = "pypy"` for marker evaluation and
accepts `ppXY-pypyXY_pp73` wheel tags instead of `cpXY`. Labels use the
`pp` interpreter prefix (`pp311-linux_x86_64`).

A CPython-only matrix leaves the axis open: its lockfile markers carry
no `implementation_name`. Any other matrix, whether it names two
implementations or one non-CPython one, puts `implementation_name` on
every target's marker and on the `environments` entry it declares. The
CPython and PyPy entries for one `(python, platform)` point stay
mutually exclusive, and a PyPy-only lock refuses CPython. PyPy's
`implementation_version` is modelled as the Python level, not PyPy's
own release, so the rare marker comparing `implementation_version`
against a PyPy version misevaluates during the resolve. The lockfile
does not carry that synthetic value: a non-CPython `environments` entry
leaves `implementation_version` open, so a real PyPy still accepts the
lock (a dep gated on the axis may be missed at install).

## Resolution axes

The `[tool.nab.matrix]` keys above drive two decisions per tuple: how
each PEP 508 marker evaluates, and which wheels the tuple can install.

### Marker variables

Every PEP 508 environment variable gets a value in every tuple, so no
marker ever evaluates against a missing key. Each takes its value from
the axis or fixed default shown:

| Marker variable | Set by | Default |
| --- | --- | --- |
| `python_version` | `python` | one value per minor in range |
| `python_full_version` | `python`, `python-patches` | `<minor>.0` |
| `implementation_version` | `python`, `python-patches` | same as `python_full_version` |
| `implementation_name` | `implementations` | `cpython` |
| `platform_python_implementation` | `implementations` | `CPython` |
| `sys_platform` | `platforms` | per id (`linux`, `darwin`, `win32`) |
| `platform_system` | `platforms` | per id (`Linux`, `Darwin`, `Windows`) |
| `platform_machine` | `platforms` | per id (`x86_64`, `aarch64`, `i686`, `armv7l`, `arm64`, `AMD64`, `ARM64`) |
| `os_name` | `platforms` | per id (`posix`, `nt`) |
| `platform_release` | `platforms` (`platform-release`) | `""` |
| `platform_version` | `platforms` (`platform-version`) | `""` |

`extra`, `extras` and `dependency_groups` are not axes. `extra` is bound
one name at a time as a version's dependencies are sorted into the base
package and its extras, so `extra == "cpu"` names the dependencies of
`pkg[cpu]` and a requirement read with no extra active sees none. The
other two are empty during resolution, so a dependency gated on
`'x' in extras` is always dropped; nab emits that clause only onto a
package's lockfile marker, where it fires for the installer consuming
the lock.

### How the axes couple

One axis usually sets several variables at once, so an impossible
combination cannot be declared:

* `platforms` sets `sys_platform`, `platform_system`,
  `platform_machine`, and `os_name` together per id. You pick
  `linux_x86_64`, not the four separately, so a Linux `sys_platform`
  can never pair with a macOS `platform_machine`.
* `implementations` sets `implementation_name` and
  `platform_python_implementation` together.
* `python` sets `python_version`, `python_full_version`, and
  `implementation_version` together.

### Wheel selection

The matrix also decides which wheels a tuple can install, computed from
the python version, platform, and implementation without a live
interpreter. A version whose only wheels are tag-incompatible with a
tuple is dropped for that tuple; a version that also ships a `.tar.gz`
sdist stays, subject to the build policy. Each tuple accepts three
wheel-tag dimensions:

* interpreter: `cpXY` for CPython, `ppXY` for PyPy, plus the
  interpreter-agnostic `py3` tags.
* abi: `cpXY` and `abi3` for CPython, `cpXYt` and `abi3t` on a
  free-threaded target, `pypyXY_pp73` for PyPy, and `none`.
* platform: manylinux or musllinux for the declared libc family, macosx,
  and win.

The tag knobs live on the platform, written as a table in `platforms`
rather than a bare id. The "Platform tag knobs" table in
[Configuration](../reference/configuration.md) lists each with its
default and the rules it carries.

### What the axes do not cover

The `[tool.nab.matrix]` keys and the platform tag knobs are the whole of
it. The platform ids and the implementations are fixed enumerations, and
an unknown name is a config error rather than a silently skipped tuple.
The Python minors are an enumeration too, but `python` is a specifier
intersected with it: a range reaching past the newest minor nab knows
expands to the ones it knows, and only a range matching none of them is
an error.

`platform_release` and `platform_version` name one machine's kernel
build. Both default to the empty string, so a marker gated on the kernel
(`platform_release >= "5.10"`) evaluates False and its dependency is
dropped: a target that does run that kernel has to declare it.
