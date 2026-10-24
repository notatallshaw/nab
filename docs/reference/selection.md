# Selecting what to lock

`nab lock` and `nab download` resolve regular project dependencies plus the extras and groups you
select. With no selection settings or flags, nab selects no project extras or dependency groups.
Workspace discovery can also supply local packages. `--build-requirements` and `--no-emit-workspace`
apply only to `nab lock`.

The commands themselves are on the [CLI](cli.md) page.

For examples of version requirements, environment markers, dependency extras and group includes, see
[Choose dependencies and conditions](../how-to/choose-dependencies.md).
[Package policy selectors](package-policy.md) select candidates for overrides separately.

## Project selection settings

Set these keys in `[tool.nab]` or the project-directory `nab.toml`:

| Key              | Default | Effect                                                                                                                             |
| ---------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `default-groups` | `[]`    | Select these groups on every resolve, unioned with CLI group selection. The names are also recorded as lockfile defaults.          |
| `base-group`     | unset   | Give the project's regular dependencies a lockfile group name so an installer can select other groups separately.                  |
| `build-group`    | unset   | Resolve the static `[build-system].requires` list alongside runtime dependencies under this lockfile group. Requires `base-group`. |

For a project declaring a `dev` group and build requirements:

```toml
[tool.nab]
default-groups = ["runtime", "dev"]
base-group = "runtime"
build-group = "build"
```

The synthetic `base-group` and `build-group` names must differ from each other and from declared
dependency groups. They are accepted in `default-groups`, but not by `--groups`. An explicit
`default-groups` replaces the implicit base-group default; include the base or build name if it
should install by default. See [Lockfile extras and groups](lockfile-groups.md) for the emitted
markers.

Two members of an at-most-one or exactly-one conflict set cannot both be defaults. Adding a
conflicting selection on the CLI can fork the resolve; see [Conflicts](../explanation/conflicts.md).

## Dependency-group selection (PEP 735)

- `--groups foo bar` folds the named groups from the project's `[dependency-groups]` table into the
  resolve. The selected group names land in the lockfile's top-level `dependency-groups` array. The
  separate `default-groups` array records the project's default selection, not this run's selection.
  Without an explicit `default-groups`, a configured `base-group` supplies that default.
- `--all-groups` selects every group defined in the project.

## Extras selection

- `--extras foo bar` folds entries from the project's own `[project.optional-dependencies]` table
  into the resolve. The selected extra names land in the lockfile's top-level `extras` array.
- `--all-extras` selects every declared extra.

## One resolve or several

`--groups` and `--extras` normally produce a single union resolve. A mutually exclusive
`[tool.nab].conflicts` set engages when two or more members are active through the selection,
`base-group`, or `build-group`. An engaged set forks the run once per allowed member combination;
see [Conflicting extras and groups](../explanation/conflicts.md).

nab adds a `'X' in extras` or `'X' in dependency_groups` marker to a package needed only by a
selected extra or group. An installer omits that package when the selection is inactive; see
[Lockfiles](lockfile.md).

(build-requirements)=

## Build requirements

- `--build-requirements` locks the project's `[build-system].requires` instead of its dependencies,
  so the lock describes the environment the project is built in rather than the one it runs in.
  `--output` defaults to `pylock.build.toml` (or `build-requirements.txt` for the requirements
  formats), which keeps it clear of the project's runtime lock, and `--locked` then checks that
  file.

  Nothing can be selected alongside it: `[build-system].requires` is one flat list, so `--groups`,
  `--all-groups`, `--extras`, `--all-extras`, `--project-default-group`, `--project-base-group` and
  `--project-build-group` are all rejected, and `[tool.nab].default-groups`,
  `[tool.nab].base-group`, `[tool.nab].build-group` and `[tool.nab].conflicts` declared in the
  project's files do not apply.

- A project with no `[build-system]` is an error. nab does not invent the PEP 517 default backend or
  pin an undeclared `setuptools`.

  `[tool.nab].build-group` has the same requirement. It also requires `[tool.nab].base-group` so
  consumers can select runtime and build pins separately.

- Only the static list is read. Neither this flag nor `build-group` invokes the project's own
  backend, so whatever that backend would add from `get_requires_for_build_wheel` is not covered.

- `[tool.nab].build-group` adds a selection marker to its packages. The two requirements formats
  cannot carry it, so they render build requirements as ordinary pins. Use `pylock` output, or this
  flag, when the two sets have to stay apart.

(workspace-flags)=

## Workspace flags

See [Lock a workspace](../how-to/workspaces.md).

- `--workspace-discovery` (default) walks upward from the locked project for a
  `[tool.nab.workspace]` root and prefers its in-tree members over PyPI. `--no-workspace-discovery`
  skips that search, so the run resolves the named project alone.

- `--no-emit-workspace` drops the workspace members' own `[[packages]]` entries from the emitted
  lockfile, along with the dependency edges and membership markers that reference them. The resolver
  still uses the members during the resolve. It is off by default.

  Use it for hashed requirements because pip's `--require-hashes` mode rejects member directory
  pins. Install those members separately with `pip install --no-deps -e <member>`. A local PEP 751
  lock can retain them.
