# Lockfile environments

A resolve drops dependencies whose PEP 508 markers are false on its [target](target-environment.md).
The resulting pins therefore belong to that environment, or to the targets declared by a matrix.

The top-level `environments` entries record that scope. A PEP 751 consumer refuses a lock that does
not cover its environment. Nab checks that the rows cover every resolved target. If a custom
declaration uses a version substring and coverage cannot be verified, use version comparisons that
describe the supported range.

## Recorded marker values

Each declaration pins `python_version`, `sys_platform`, and `platform_machine`. It adds
`implementation_name` for a non-CPython target or a matrix with several implementations.

It also pins each PEP 508 variable consulted by a dependency, root requirement, or constraint
marker. This prevents a consumer that answers a relevant marker differently from using the lock. The
variables below are exceptions.

## Patch-release boundaries

A target naming a Python minor covers its patch releases. Pinning `python_full_version` or
`implementation_version` to a single patch would exclude the other releases.

For a target spanning Python 3.13, this requirement changes at a patch release:

```toml
[project]
dependencies = [
    'pytest ; python_full_version >= "3.13.4"',
]
```

The dependency above first applies on 3.13.4. It is absent on 3.13.4rc1. The lower row uses
`<= "3.13.4"` together with `!= "3.13.4"` to include prereleases of that patch. The upper row uses
`>= "3.13.4"`.

A dependency with `python_full_version < "3.13.4"` changes earlier. PEP 440 excludes prereleases of
3.13.4 from that comparison, so its upper row starts at 3.13.4.dev0.

### CPython implementation versions

On CPython, `implementation_version` matches `python_full_version` and can split the minor the same
way. A slice that consulted it carries both sets of bounds; otherwise it carries only
`python_full_version`.

For example, both variables use the final-release boundary for `implementation_version >= "3.13.4"`.
Nab checks those bounds against the same CPython version. On PyPy the implementation version is
independent of the Python language version.

### Local Python builds

When a marker compares a literal to the running version, a local build can select different
dependencies from the public release. For example, `"3.12.4" == python_full_version` is true on
3.12.4 and false on 3.12.4+vendor. Nab resolves both builds when the comparison distinguishes them.

The marker `"+" in python_full_version` selects local builds across the minor. Its negation selects
public releases.

### Unsplit targets

If every consulted version marker has the same result throughout Python 3.13, the row needs only
`python_version == "3.13"` for its Python bound. For example, `python_full_version >= "3.12"` holds
throughout that minor.

To resolve one patch release without splitting it:

```toml
[tool.nab.matrix.python-patches]
"3.13" = "3.13.4"
```

With this pin, nab resolves Python 3.13.4 once and records `python_full_version == "3.13.4"` in the
lock. An installer on 3.13.5 needs a lock that includes its patch release. If a dependency reads the
full Python version or the CPython implementation version, the row also excludes local builds. To
resolve for a build such as `3.13.4+vendor`, give its full version in `python-patches`. For a host
lock, nab records the patch interval where the consulted version markers give the same answers as on
the running interpreter. A marker that cannot be split into these intervals limits the lock to the
observed build.

If a dependency uses `in` or `not in` on the full Python version, two PEP 440-equivalent versions
can give different answers. For example, `"vendor" in python_full_version` distinguishes
`3.13.4+vendor` from `3.13.4+VENDOR`. A lock for an exact patch or the host preserves that
membership test in its environment declaration.

### Unsupported boundaries

On a minor-interval target, a consulted full-version marker must describe intervals. Nab rejects:

- membership other than testing for a local label (`"+" in python_full_version` or its negation),
  verbatim `===`, non-version strings, and comparisons with another variable;
- invalid PEP 440 operands such as `< "3.12.*"` or `~= "3"`;
- local version labels within the target minor, such as `python_full_version == "3.12.4+vendor"`;
- the pre- and post-release boundaries listed below.

For a literal above the first final release in the minor, a prerelease is an error on `<`, `>=`,
`==`, `!=` and `~=`, and a literal that is only a post-release on `>=`, `==`, `!=` and `~=`. For
example, `> "3.12.4rc1"` starts at 3.12.4rc2.dev0, and `> "3.12.4.post1"` starts at 3.12.5.dev0.

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
