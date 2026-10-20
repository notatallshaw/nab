# Choose dependencies and conditions

Use requirements and markers to choose what each target needs, then extras and groups to choose
optional sets. These choices apply to `nab lock` and `nab download`.

## Add requirements for particular targets

Declare requirements under `[project].dependencies`. A version specifier limits releases; a marker
after `;` selects environments. See
[dependency specifiers](https://packaging.python.org/en/latest/specifications/dependency-specifiers/)
for syntax and marker variables.

This complete project needs Colorama only on Windows and Tomli only on Python 3.10:

```toml
[project]
name = "example"
version = "0.1.0"
requires-python = ">=3.10,<3.12"
dependencies = [
    "colorama>=0.4 ; sys_platform == 'win32'",
    "tomli>=2 ; python_version < '3.11'",
]

[tool.nab]
mode = "universal"

[tool.nab.matrix]
python = ">=3.10,<3.12"
platforms = ["linux_x86_64", "windows_amd64"]
```

Run `nab lock` to cover all four targets. Linux 3.11 needs neither package; Windows 3.10 needs both.
The lock records environment conditions for their entries.

Markers are evaluated for each target, including markers in transitive dependencies. The machine
running nab does not decide whether a Windows target gets its Windows dependencies. See
[Universal resolution](../explanation/universal.md) for patch-version markers and
[Target environment](../reference/target-environment.md) for one deployment target.

## Request an extra from a dependency

An extra in a requirement asks a dependency for an optional feature:

```toml
[project]
name = "example"
version = "0.1.0"
dependencies = ["requests[socks]>=2,<3"]
```

This requests Requests' `socks` dependencies. `nab lock --extras socks` instead asks for an extra
named `socks` on your own project.

If the chosen dependency version lacks a directly requested extra, nab reports an error. A missing
extra requested only by a transitive dependency produces a warning and adds no extra dependencies.

## Select your project's extras and groups

Add optional application features under `[project.optional-dependencies]` and development dependency
sets under `[dependency-groups]`. The
[project metadata](https://packaging.python.org/en/latest/specifications/pyproject-toml/) and
[dependency groups](https://packaging.python.org/en/latest/specifications/dependency-groups/)
specifications define their formats:

```toml
[project.optional-dependencies]
cli = ["rich>=13"]
all = ["example[cli]"]

[dependency-groups]
test = ["pytest>=8"]
dev = [{ include-group = "test" }, "ruff"]
```

Then select the sets to resolve:

```bash
nab lock --extras cli --groups dev
```

The project's regular dependencies are included too. `--all-extras` and `--all-groups` select every
declared extra or group. `[tool.nab].default-groups` selects groups on every resolve.

### Include other extras

An extra can include another extra from the same project. Here `--extras all` also selects `cli`
through `example[cli]`. Use the project's declared name for this self-reference; markers on it are
evaluated per target too.

### Conflicts and installation

Selected sets normally resolve together, so their requirements must be compatible. Declare
[conflicting extras or groups](../explanation/conflicts.md) when they are alternatives that need
separate pins in one lock.

The lock can offer extras and groups for an installer to select. Check the installer's selection
support: pip currently uses default groups and no extras.
[Lockfile extras and groups](../reference/lockfile-groups.md) explains those conditions and how
`base-group` and `build-group` separate runtime and build dependencies.

## Constrain versions without adding a dependency

A constraint narrows a package only if the dependency graph already needs it. It can carry a marker:

```toml
[tool.nab]
constraints = ["urllib3<2 ; python_version < '3.11'"]
```

This caps urllib3 on older targets without introducing it, or limiting newer targets. Use
`--project-constraint` for one run. See [Resolution settings](../reference/resolution-settings.md)
for accepted constraint syntax and version preferences.

## Select packages for a policy override

[Package policy selectors](../reference/package-policy.md) have a different scope: they select a
package name and optional version range to receive a policy or metadata replacement. For example,
`[tool.nab.packages."numpy<2"]` applies only to those candidate versions. These selectors accept
neither extras nor markers; put environment conditions on dependency requirements.
