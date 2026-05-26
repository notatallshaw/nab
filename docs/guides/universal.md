# Universal resolution

> [!WARNING]
> Universal mode is experimental. The resolver loop and the PEP
> 751 lockfile shape it produces are subject to change.

A specific resolve pins one version per package for one marker
environment. A universal resolve produces a single artefact
valid for a set of marker environments.

nab's universal-resolution model is user-driven: the user
declares their target Python range and platform list in
`[tool.nab.matrix]`, and the resolver only ever considers what
they declared.

## How it works

For each `(python, platform)` cell in the matrix, nab runs the
single-environment resolver against an impersonated marker
environment. All cells share one fetcher, so each package's
metadata is fetched at most once across the whole matrix. After
each cell resolves, its pins flow forward as preferences for the
next cell, providing best-effort cross-tuple alignment.

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

`python` is a PEP 440 specifier expanded into one tuple per
minor version. `platforms` is a list of platform ids
(`linux_x86_64`, `linux_aarch64`, `macos_x86_64`, `macos_arm64`,
`windows_amd64`).

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
Packages whose pinned version differs across tuples appear as
multiple `Package` entries with PEP 508 markers; packages that
agree across every tuple appear once with no marker.

## Inspect the per-tuple pins

```bash
nab lock --format requirements-without-hashes --output - pyproject.toml
```

```
warning: mode = 'universal' is experimental; output format may change without notice
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

One block per `(python, platform)` cell. Pip cannot install a
single requirements.txt across multiple tuples in hash-checking
mode, so the per-tuple block format is for inspection or for
tools that consume one block at a time. When a tuple fails
resolution, the line reads `# <label>: FAILED` followed by the
indented error and the process exits 1.

## Patch-release markers

`python_full_version` defaults to `<minor>.0` per cell. If a
marker in the dependency graph compares against a patch release,
declare the real patch you ship on:

```toml
[tool.nab.matrix.python-patches]
"3.11" = "3.11.4"
"3.12" = "3.12.1"
```

Without this, markers like `python_full_version >= "3.11.4"`
evaluate False on a 3.11 cell, the safe direction (drop the
gated dep) but a silent failure if your deployed interpreter is
actually 3.11.4 or later.

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

Each implementation multiplies the tuple count (pythons x platforms x
implementations). A PyPy tuple sets `platform_python_implementation =
"PyPy"` / `implementation_name = "pypy"` for marker evaluation and
accepts `ppXY-pypyXY_pp73` wheel tags instead of `cpXY`. Labels use the
`pp` interpreter prefix (`pp311-linux_x86_64`).

The CPython tuple's lockfile marker stays unconstrained
(`python_version`, `sys_platform`, `platform_machine` only) for
backward compatibility; a non-CPython tuple adds an
`implementation_name` clause so its entry is distinguishable. PyPy's
`implementation_version` is modelled as the Python level, not PyPy's
own release, so the rare marker comparing `implementation_version`
against a PyPy version misevaluates.

## Trade-offs versus marker-fork PubGrub

| Property | Matrix (nab) | Marker-fork (uv) |
| --- | --- | --- |
| Resolver core | Untouched. Universal is a per-tuple loop on top of the existing single-env resolver. | Forks pervade the resolver state. |
| Universe | Exactly what the user declared. | All of PEP 508, narrowed by `tool.uv.environments`. |
| Errors | "no wheel for python 3.11 on macos arm64" - actionable. | "no wheel for some marker environment the resolver cared about" - less actionable. |
| Wasted work | High: 90% of resolver state is identical across tuples; not shared. | Low: shared whenever markers don't fork. |
| Implementation cost | Small. Promotes a working PoC. | Large. Conflict explanations and lockfile shape are research-grade work. |
