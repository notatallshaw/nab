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

The targets share one fetcher, so each package's metadata is fetched at
most once across the whole matrix. After a target resolves, its pins
flow forward as preferences for the next, giving best-effort alignment
across targets.

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
pinned only on the lower slice, so it carries that slice's marker and is
absent from the other:

```toml
[[packages]]
name = "some-backport"
version = "1.2.0"
marker = 'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and python_full_version < "3.11.4"'
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
prerelease literals strictly inside the minor.

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
`python_version`, `sys_platform` and `platform_machine` only. Any other
matrix, whether it names two implementations or one non-CPython one,
puts `implementation_name` on every target's marker and on the
`environments` entry it declares. The CPython and PyPy entries for one
`(python, platform)` point stay mutually exclusive, and a PyPy-only
lock refuses CPython. PyPy's `implementation_version` is modelled as
the Python level, not PyPy's own release, so the rare marker comparing
`implementation_version` against a PyPy version misevaluates during the
resolve. The lockfile does not carry that synthetic value: a non-CPython
`environments` entry leaves `implementation_version` open, so a real PyPy
still accepts the lock (a dep gated on the axis may be missed at install).

## Trade-offs versus marker-fork PubGrub

| Property | Matrix (nab) | Marker-fork (uv) |
| --- | --- | --- |
| Resolver core | Untouched. A matrix is a longer target list, not extra solver state. | Forks pervade the resolver state. |
| Universe | Exactly what the user declared. | All of PEP 508, narrowed by `tool.uv.environments`. |
| Errors | "no wheel for python 3.11 on macos arm64" - actionable. | "no wheel for some marker environment the resolver cared about" - less actionable. |
| Wasted work | High: solver state is rebuilt per target. Only the fetcher and its metadata cache are shared. | Low: shared whenever markers don't fork. |
| Implementation cost | Small. One engine over a target list. | Large. Conflict explanations and lockfile shape are research-grade work. |
