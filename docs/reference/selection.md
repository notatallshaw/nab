# Selecting what to lock

Which dependency groups and extras a `nab lock` or `nab download` run
covers, and what a workspace adds to it. `--build-requirements` and
`--no-emit-workspace` are `nab lock` only.

The commands themselves are on the [CLI](cli.md) page.

## Dependency-group selection (PEP 735)

* `--groups foo bar` folds the named groups from the project's
  `[dependency-groups]` table into the resolve. The selected
  group names land in the lockfile's top-level
  `dependency-groups` array. The separate `default-groups` array
  records the `[tool.nab].default-groups` project setting, not
  this run's selection, plus the group named by
  `[tool.nab].base-group` when it is set.
* `--all-groups` selects every group defined in the project.

## Extras selection

* `--extras foo bar` folds entries from the project's own
  `[project.optional-dependencies]` table into the resolve. The
  selected extra names land in the lockfile's top-level `extras`
  array.
* `--all-extras` selects every declared extra.

## One resolve or several

Both `--groups` and `--extras` produce a single union resolve, unless
two or more members of a mutually exclusive `[tool.nab].conflicts` set
are active, either because the selection names them or because they are
the groups `base-group` and `build-group` name, which are active on
every run. The run then forks into one resolve per choice of member;
see [Conflicting extras and groups](../explanation/conflicts.md).

A package that only a selected extra or group reaches is emitted with
a `'X' in extras` or `'X' in dependency_groups` marker, so an
installer given neither leaves it out; see
[Lockfiles](lockfile.md).

## Build requirements

* `--build-requirements` locks the project's `[build-system].requires`
  instead of its dependencies, so the lock describes the environment
  the project is built in rather than the one it runs in. `--output`
  defaults to `pylock.build.toml` (or `build-requirements.txt` for the
  requirements formats), which keeps it clear of the project's runtime
  lock, and `--locked` then checks that file.

  Nothing can be selected alongside it: `[build-system].requires`
  is one flat list, so `--groups`, `--all-groups`, `--extras`,
  `--all-extras`, `--project-default-group`, `--project-base-group` and
  `--project-build-group` are all rejected, and `[tool.nab].default-groups`,
  `[tool.nab].base-group`, `[tool.nab].build-group` and
  `[tool.nab].conflicts` declared in the project's files do not apply.
* A project that declares no `[build-system]` is an error. nab does not
  fall back to the PEP 517 default backend, because pinning an implied
  `setuptools` would put a build requirement in the lock that the project
  never declared. `[tool.nab].build-group`, which carries the build
  requirements in the project's own lock instead of a separate one, is
  the same, and it requires `[tool.nab].base-group` so the two sets can
  be asked for separately.
* Only the static list is read. Neither this flag nor `build-group`
  invokes the project's own backend, so whatever that backend would add
  from `get_requires_for_build_wheel` is not covered.
* `[tool.nab].build-group` gates its packages on a marker, and the two
  requirements formats have nowhere to put one, so they render the build
  requirements as ordinary pins. Use `pylock` output, or this flag, when
  the two sets have to stay apart.

## Workspace flags

See [Lock a workspace](../how-to/workspaces.md).

* `--workspace-discovery` (default) walks upward from the locked
  project for a `[tool.nab.workspace]` root and prefers its in-tree
  members over PyPI. `--no-workspace-discovery` skips that search, so
  the run resolves the named project alone.
* `--no-emit-workspace` drops the workspace members' own `[[packages]]`
  entries from the emitted lockfile, along with the dependency edges
  and membership gates that reference them; the resolver still uses the
  members during the resolve. It is off by default.

  Use it for hashed requirements because pip's `--require-hashes` mode
  rejects member directory pins. Install those members separately with
  `pip install --no-deps -e <member>`. A local PEP 751 lock can retain them.
