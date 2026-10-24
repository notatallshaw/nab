# Lockfile environments

A resolve drops dependencies whose PEP 508 markers are false on its [target](target-environment.md).
The resulting pins therefore belong to that environment, or to the targets declared by a matrix.

The top-level `environments` entries record that scope. A PEP 751 consumer refuses a lock that does
not cover its environment. nab also refuses to emit a lock whose declaration misses a resolved
target and names the uncovered interpreter.

## Recorded marker values

Each declaration pins `python_version`, `sys_platform`, and `platform_machine`. It adds
`implementation_name` for a non-CPython target or a matrix with several implementations.

It also pins each PEP 508 variable consulted by a dependency, root requirement, or constraint
marker. This prevents a consumer that answers a relevant marker differently from using the lock. The
variables below are exceptions.

## Patch-release boundaries

`python_full_version` and `implementation_version` are not declared as one micro version. A target
naming a minor stands for every micro in that minor, so a single value would reject the others.

For a target spanning Python 3.13, this requirement changes at a patch release:

```toml
[project]
dependencies = [
    'pytest ; python_full_version >= "3.13.4"',
]
```

A consulted `python_full_version` boundary inside the minor splits it into slices. On a 3.13 target,
`pytest ; python_full_version >= "3.13.4"` creates `< "3.13.4"` and `>= "3.13.4.dev0"` slices; only
the upper one includes `pytest`.

The bounds meet at `3.13.4.dev0` with no gap or overlap. A verbatim `< "3.13.4"` / `>= "3.13.4"`
pair would exclude prereleases such as `3.13.4rc1` from both slices. Starting the upper slice at
`3.13.4.dev0` gives those interpreters the 3.13.4 pins.

### CPython implementation versions

On CPython, `implementation_version` matches `python_full_version` and can split the minor the same
way. A slice that consulted it carries both sets of bounds; otherwise it carries only
`python_full_version`.

For example, `implementation_version >= "3.13.4"` gives the lower slice matching `< "3.13.4"`
clauses and the upper slice matching `>= "3.13.4.dev0"` clauses for both variables.

### Unsplit targets

A minor with no marker boundary uses a plain `python_version == "3.13"` row with no
`python_full_version` clause: a marker whose boundary lies outside the minor, or a prerelease of the
minor's floor (`<= "3.11.0a6"`), reads the same for every real micro, so it names no in-minor
boundary and its dependency is simply kept or dropped for the whole minor.

To resolve one patch release without splitting it:

```toml
[tool.nab.matrix.python-patches]
"3.13" = "3.13.4"
```

nab does not split a target pinned to an exact patch release, including the host interpreter and
`[tool.nab.matrix.python-patches]` entries. It resolves that release once and records a
`python_version == "X.Y"` environment row.

### Unsupported boundaries

On a minor-interval target, a consulted full-version marker must describe intervals. nab rejects:

- membership (`in` or `not in`), verbatim `===`, non-version strings, and comparisons with another
  variable;
- invalid PEP 440 operands such as `< "3.12.*"` or `~= "3"`;
- the pre- and post-release boundaries listed below.

A prerelease literal is an error on `<`, `>=`, `==`, `!=` and `~=`, and a literal that is only a
post-release on `>=`, `==`, `!=` and `~=`. Every other operator lands the boundary on a real micro:
`<= "3.12.4rc1"` and `> "3.12.4rc1"` cut at 3.12.4, and `< "3.12.4.post1"`, `<= "3.12.4.post1"` and
`> "3.12.4.post1"` at 3.12.5.

## Unrecorded marker values

`platform_release` and `platform_version` are never declared because they name one machine's kernel
build. Recording the resolving machine would reject other machines.

A non-CPython target also drops `implementation_version`. nab models it as the Python level, while
released PyPy reports its own version, so recording the modelled value would reject the intended
interpreter. This axis does not split the target's minor.

nab warns when a marker uses an unrecorded variable: one warning covers the kernel variables and
another covers `implementation_version` on non-CPython targets. An installer can accept the lock
with a different value for that variable and miss a dependency needed in its environment.

## Project Python support

`requires-python` is the project's declaration (`[tool.nab].requires-python`, or
`[project].requires-python`), not the target's Python. It bounds what the project supports; the
`environments` entries name what was resolved.

## Universal mode

Under `[tool.nab].mode = "universal"`, one pylock covers every matrix target. Pins can share an
entry across targets. Entries that apply to only some targets carry environment markers; a split
minor also uses `python_full_version` bounds. [Extras and group markers](lockfile-groups.md) still
apply to selection-specific packages.

nab shortens each marker only after checking equivalence over every declared environment. It keeps
the original marker if simplification or verification runs out of budget, or if the marker selects
no declared environment.

`environments` is built from the marker variables each target consulted. A minor split at a micro
boundary contributes one declaration per slice.

An entry's `packages.dependencies` edges are the union across every target it covers. See
[Universal resolution](../explanation/universal.md).

See [Target environment](target-environment.md) and [Target matrix](target-matrix.md) to choose the
targets.
