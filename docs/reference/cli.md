# CLI

`nab` exposes four subcommands: `lock`, `download`, `config`, and
`cache`. The first two read project shape from `[tool.nab]` in the
project's `pyproject.toml` or a project-directory `nab.toml`; the CLI
carries runtime knobs and can override a project key for one run with a
`--project-<key>` flag. `config` inspects the layered configuration, and
`cache` inspects and clears the on-disk cache.

## Synopsis

```
nab [--version | -V]
nab [OUTPUT FLAGS] lock     [PATH] [RUNTIME OPTIONS] [--output PATH] [--format pylock|requirements|requirements-without-hashes]
nab [OUTPUT FLAGS] download [PATH] [RUNTIME OPTIONS] [--output DIR] [--max-concurrency N]
nab [OUTPUT FLAGS] config   {list | get <key> | explain <key>} [--path PATH]
nab [OUTPUT FLAGS] cache     {dir | verify | clear} [--cache-dir PATH]
```

`OUTPUT FLAGS` are the global verbosity, colour, and progress knobs listed
under Output control below. They are read before the subcommand, so they may
appear anywhere on the line.

`PATH` is positional and defaults to `pyproject.toml` in the
current directory. Run `nab lock --help` (or `-h`) for the full
per-command flag list. Boolean flags render as a `--flag` /
`--no-flag` pair (for example `--cache` / `--no-cache`). `--offline`
is layered, so an explicit `--offline True` / `--offline False`
overrides the config layers; bare `--offline` / `--no-offline` are
shorthands for those two values.

## `nab lock`

Resolve and emit a lockfile or pin list. Three formats:

* `--format pylock` (default) writes a [PEP 751] `pylock.toml`.
* `--format requirements` writes a pip-compatible
  `requirements.txt` with one `--hash=<algo>:<digest>` line per
  recorded digest.
* `--format requirements-without-hashes` writes a sorted
  `name==version` list with no hashes.

Dependency-group selection (PEP 735):

* `--groups foo bar` folds the named groups from the project's
  `[dependency-groups]` table into the resolve. The selected
  group names land in the lockfile's top-level
  `dependency-groups` array. The separate `default-groups` array
  records the `[tool.nab].default-groups` project setting, not
  this run's selection, plus the group named by
  `[tool.nab].base-group` when it is set.
* `--all-groups` selects every group defined in the project.

Extras selection:

* `--extras foo bar` folds entries from the project's own
  `[project.optional-dependencies]` table into the resolve. The
  selected extra names land in the lockfile's top-level `extras`
  array.
* `--all-extras` selects every declared extra.

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

Build requirements:

* `--build-requirements` locks the project's `[build-system].requires`
  instead of its dependencies, so the lock describes the environment
  the project is built in rather than the one it runs in. `--output`
  defaults to `pylock.build.toml` (or `build-requirements.txt` for the
  requirements formats), which keeps it clear of the project's runtime
  lock, and `--locked` then checks that file. Nothing can be selected alongside it: `[build-system].requires`
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

Workspace flags (see [Lock a workspace](../how-to/workspaces.md)):

* `--workspace-discovery` (default) walks upward from the locked
  project for a `[tool.nab.workspace]` root and prefers its in-tree
  members over PyPI. `--no-workspace-discovery` skips that search, so
  the run resolves the named project alone.
* `--no-emit-workspace` drops the workspace members' own `[[packages]]`
  entries from the emitted lockfile, along with the dependency edges
  and membership gates that reference them; the resolver still uses the
  members during the resolve. Default off (`--no-no-emit-workspace`). Use it
  when the lockfile is consumed by pip's PEP 751 install or
  `--require-hashes`, which reject the directory entry a member pin
  emits because it cannot be hashed; pair it with `pip install
  --no-deps -e <member>`.

`--output` defaults to `pylock.toml` for `pylock` and
`requirements.txt` for the two requirements formats, or to
`pylock.build.toml` and `build-requirements.txt` under
`--build-requirements`. Pass
`--output -` to write to stdout instead. A matrix has no default
requirements file: no one file can carry every tuple's pins (see
below), so the requirements formats print to stdout unless `--output`
names a template.

A requirements `--output` template writes one file per tuple. The
variables are `{python_version}`, `{platform_id}`, and `{selection}`,
which names the conflict fork a tuple belongs to (`extra-cpu`,
`group-black22.group-isort5`, empty when the resolve did not fork):

```console
$ nab lock --format requirements-without-hashes --extras cpu gpu \
    --output 'req-{python_version}-{selection}.txt'
Wrote req-3.12-extra-cpu.txt (12 packages, tuple py312-linux_x86_64-extra-cpu)
Wrote req-3.12-extra-gpu.txt (14 packages, tuple py312-linux_x86_64-extra-gpu)
```

A template that maps two tuples onto one path is rejected, naming the
variables that would give every tuple its own file. When the tuples
split on an axis no variable names, such as the libc or the
implementation, no template separates them and the message points at
pylock output instead.

Exits non-zero on resolution failure; the message starts with
`error: resolution failed:` followed by a derivation tree, and any
captured diagnostics are appended under a `Diagnostics:` section. A
package the resolve found no candidate for gets one line there naming
every filter that refused its files, how many each refused, and the
version the evidence comes from. Where a config key would admit them
again, an indented `note:` line names it and what to set:

```
Diagnostics:
  - foo: found on index but no distribution is compatible: the uploaded-prior-to cutoff 2026-05-01T00:00:00+00:00 excluded 1 file uploaded at 2030-01-01T00:00:00Z (1.0); no sdist is available to build from
    note: the project-level uploaded-prior-to set that cutoff; uploaded-prior-to = false under [tool.nab.packages."foo"] lifts it for this package
```

A remedy is offered for the upload-time cutoff alone. A release the
requirement asked for that a filter dropped reads as `found on index
but every version matching the requirement was filtered`, naming those
filters.

Universal mode (`[tool.nab].mode = "universal"`) is supported for
all three formats:

* `pylock` produces one PEP 751 file with per-tuple `Package`
  entries gated by markers (`python_version`, `sys_platform`,
  `platform_machine`, plus `implementation_name` when the matrix
  declares a non-CPython implementation or more than one). Versions
  agreed across every tuple appear once without a marker; divergent
  versions appear once per `(version, source)` group with the
  matching tuples disjoined.
* `requirements` and `requirements-without-hashes` emit a
  sequence of `# label` comment blocks, one per
  `(python, platform, implementation)` tuple, followed by that
  tuple's pins. Pip's hash-checking mode cannot install a single
  requirements.txt across multiple tuples, so the per-tuple block
  format is for inspection or for tools that consume one block at
  a time.

Failed tuples render as `# {label}: FAILED` followed by the error,
every line of it commented and indented, and exit `1`.

`--python X.Y` resolves for that Python on this machine instead of the
running interpreter, like pip's `--python-version`; it moves only the
python axis, so a declared `[tool.nab.environment].platform` stays. It is
rejected in universal mode, where the matrix declares the Python axis.

A project option can be overridden for one run with a `--project-<key>`
flag: `--project-resolution`, `--project-mode`, `--project-requires-python`,
`--project-uploaded-prior-to`, `--project-dist-policy`,
`--project-build-policy`, `--project-build-requires-depth`,
`--project-decision-order`, `--project-base-group`,
`--project-build-group`, and the
repeatable `--project-constraint` and `--project-default-group`. Every one
of them replaces the file value outright; repeating `--project-constraint`
builds up that run's whole constraint list rather than adding to the
declared one. Each changes what the run writes, so passing one prints a
reproducibility notice on stderr and records the override in the
lockfile's `[tool.nab]` block, since the lock no longer derives from the
committed files alone.

`--locked` re-resolves and checks that the committed `pylock.toml` is
already up to date, writing nothing. It exits non-zero if the lock would
change or is missing, so CI can assert the lock is current. It covers
`pylock` output to a file in single-environment mode.

When a mismatch is provable from the inputs alone, a changed direct
dependency, a changed `[build-system].requires` under `build-group`, a
narrowed `requires-python`, a changed extra or group, or a tightened
constraint, `--locked` fails fast with that reason before
resolving. Otherwise it runs the full re-resolve, and only that comparison
reports the lock up to date: nab is non-sticky, so a lock can satisfy every
input yet be stale once a newer admissible version exists.

`--upgrade` re-anchors the `P<n>D` resolve window to the current time
instead of reusing the timestamp recorded in an existing lockfile, and
prints a notice naming the cutoff it dropped.

## `nab download`

Resolve and download every wheel, sdist, and direct-URL
archive into a local directory. The download is idempotent:
files whose recorded sha256 matches a local file are left
alone. Local and VCS pins are skipped.

Universal mode (`[tool.nab].mode = "universal"`) re-resolves
across the matrix and downloads the union of every tuple's
artefacts into the same directory, deduplicated by URL so a
wheel shared across tuples is fetched once.

* `--output` defaults to `wheels/`.
* `--max-concurrency` controls parallel HTTP fetches (default `8`,
  minimum `1`). Layered, so it can also be set in an `nab.toml` or
  `NAB_MAX_CONCURRENCY`.
* `--groups foo bar` / `--all-groups` and `--extras foo bar` /
  `--all-extras` fold dependency groups and extras into the resolve as
  they do on `nab lock`, so they decide which artefacts are downloaded.
* `--python X.Y` resolves for that Python on this machine instead of
  the running interpreter, as on `nab lock`. It is rejected in
  universal mode, where the matrix declares the Python axis.
* `--workspace-discovery` (default) mirrors `nab lock`: it finds a
  `[tool.nab.workspace]` root and resolves against the in-tree
  members. `--no-workspace-discovery` turns that off. See
  [Lock a workspace](../how-to/workspaces.md).

`--offline`, `--cache-dir`, `--http-backend`, `--max-concurrency` and the
`--project-*` overrides flow through the same layered config sources `nab
lock` uses, so a `NAB_*` variable or a system/user/project `nab.toml` is
read for `nab download` as for `nab lock`.

Offline covers the artefacts too: an artefact that is neither already in
the output directory with a matching digest nor readable from a local
`file://` path fails the run instead of being fetched.

A summary of how many files were written and how many were
already present is printed to stderr.

## `nab config`

Inspect the effective layered configuration (read-only). Three
actions:

* `nab config list` prints every option with its value, scope, and
  the source it came from.
* `nab config get <key>` prints one effective value.
* `nab config explain <key>` prints the full source stack for one key.
  The winning row carries a `>` gutter and the status `winner`, and
  every source it beats is `shadowed`. A shadowed source contributes
  nothing to the value, whatever the key's type.

`--include-rejected` is a flag on `nab config` itself, so every action
takes it. Without it, a config file that sets an unknown key or a key
its scope does not allow is a config error: the inspector writes the
message to stderr, prints no configuration, and exits 1. With the flag
the run succeeds, and each action shows the refused sources differently:

* `nab config list --include-rejected` prints the option table, then a
  `rejected:` section with one line per refused source: a key set
  outside its scope, an unknown key, and a `NAB_*` variable that is
  unknown or names a project-scope option. Without the flag those
  variables are stderr warnings instead.
* `nab config explain <key> --include-rejected` adds a `rejected` row
  for every source that tried to set that key and was not allowed. A
  refusal that names no option (an unknown key, or an unrecognised
  `NAB_*` variable) belongs under no key, so only `nab config list`
  shows it: on `explain` such a variable loses its stderr warning and
  gains no row.
* `nab config get <key> --include-rejected` prints the value and nothing
  else. `get` renders no refusal at all, so the flag only decides
  whether the command runs, and takes those `NAB_*` warnings off stderr
  without printing anything in their place.

The same per-option override flags the run commands accept (the
`--project-*` overrides and the user knobs `--offline`, `--cache-dir`,
`--http-backend`, `--max-concurrency`) layer a CLI value on top, so the
inspector reflects the same effective values a run would see.

See [Configuration](configuration.md) for the source ladder and the
`NAB_*` environment variables.

## `nab cache`

Inspect and clear the on-disk cache. It takes one location selector,
`--cache-dir PATH`. Without it the root is the one a run in the same
directory uses: `cache-dir` is read off the config source ladder, so a
`nab.toml` or `NAB_CACHE_DIR` sets it too (see
[Configuration](configuration.md)). Three actions:

* `nab cache dir` prints the resolved cache root to stdout, whether or
  not it exists yet.
* `nab cache verify` walks the cached index records read-only and
  reports any corrupt entry by path and reason. Cloned repositories and
  extracted archives hold upstream files, so they are not parsed.
* `nab cache clear` removes every bucket under the root, including the
  cloned repositories and extracted archives, returning the cache to
  cold.

## Runtime flags

`lock` and `download` accept the same runtime knobs:

| Flag | Default | Effect |
| ---- | ------- | ------ |
| `--cache-dir PATH` | `~/.cache/nab` | Override the on-disk cache root. |
| `--no-cache` | off | Disable cache for this run. A declared VCS or archive source is materialised into a temporary directory instead, so it is refetched every run. Combine with `--offline` only if the cache already has every file. |
| `--offline {True,False}` | unset | Use cache only, never hit the network. Layered: `--offline True` forces offline, `--offline False` forces network even over a lower `offline = true`. Bare `--offline` / `--no-offline` are shorthands for `True` / `False`. |
| `--http-backend {urllib3,httpx}` | `urllib3` | Pick the async transport for fetches. Layered, so it can also be set in an `nab.toml` or `NAB_HTTP_BACKEND`. `httpx` needs its extra (see [Install nab](../how-to/install.md)). |

A name absent from the index is remembered for a short window, so a
repeated lookup is answered from cache, offline included.

Offline refuses rather than fetches, so a PEP 517 build environment
that has anything to install cannot be created and the build fails.
At `build-remote` that rejects only the sdist version, and the
resolve tries the next candidate. A declared local, VCS, or archive
source, or a workspace member, is the only candidate for its name, so
the same refusal ends the run. See [Build policy](build-policy.md).

`urllib3` is the only backend pulled in by the base install. The
others surface a helpful `ImportError` if selected without the
matching extra.

A cache root nab cannot write to (read-only, full, over quota) does not
stop an index fetch. The run warns once and carries on, serving what the
index cache already holds and storing nothing new. Cloning a VCS
requirement or unpacking a URL archive still needs a writable cache root
and fails without one.

## Global flags

| Flag | Effect |
| ---- | ------ |
| `--version`, `-V` | Print `nab <version>` and exit `0`. |
| `--help`, `-h` | Standard argparse-style help. Per-subcommand help works too: `nab lock --help`. |

The verbosity, colour, and progress flags below are also global.

## Output control

These flags set how much `nab` writes to stderr, whether it colours it,
and whether it animates a progress line. They are global: `nab` reads them
before the subcommand, so they work with `lock`, `download`, `config`, and
`cache`. stdout carries only the requested output (the lockfile, the
requirements list, the `config` dump), so it stays pipeable at every
verbosity.

| Flag | Effect |
| ---- | ------ |
| `-v`, `-vv`, `--verbose` | Raise verbosity. `-v` adds the engine's `INFO` records, `-vv` adds `DEBUG`. `--verbose` counts as one `-v`; repeats add, and `-vvv` saturates at `-vv`. |
| `-q`, `-qq`, `--quiet` | Lower verbosity. `-q` drops the run summary and notes, keeping warnings and errors; `-qq` keeps only errors. `--quiet` counts as one `-q`. |
| `--color` | When to colour stderr: `auto` (default), `always`, or `never`. `auto` colours only an stderr terminal and honours `NO_COLOR`, `FORCE_COLOR`, and `TERM=dumb`; `always` and `never` win outright. Colour touches only a message's leading token, so it reads the same stripped. |
| `--no-color` | Shorthand for `--color never`. |
| `--no-progress` | Suppress the live progress line (also `NAB_NO_PROGRESS`). |

Verbosity is the count of `-v` minus the count of `-q`. The five levels,
quietest first, are silent (`-qq`), quiet (`-q`), normal (the default),
verbose (`-v`), and debug (`-vv`). Errors print at every level; warnings
print unless `-qq`; the run summary and notes print at normal and above.

While `nab lock` or `nab download` resolves, a live line repaints on
stderr, counting package listings fetched and packages pinned:

```
Resolving... 12 fetched, 5 pinned
```

It shows only at normal verbosity on an stderr terminal; `--no-progress`
(or `NAB_NO_PROGRESS`) turns it off, and it never writes to stdout.

## Environment variables

| Variable | Effect |
| -------- | ------ |
| `XDG_CACHE_HOME` | If set, `nab`'s default cache root is `$XDG_CACHE_HOME/nab` instead of `~/.cache/nab`. |
| `NAB_VERBOSITY` | Default verbosity when no `-v` / `-q` flag is given: one of `silent`, `quiet`, `normal`, `verbose`, `debug`. A `-v` / `-q` flag overrides it. An unrecognised value is rejected. |
| `NAB_NO_PROGRESS` | If set to a non-empty value, suppress the live progress line, like `--no-progress`. |
| `NO_COLOR` | If set to a non-empty value, disable colour under `--color auto`. |
| `FORCE_COLOR` | If set to a non-empty value, force colour under `--color auto`. |
| `TERM` | `dumb` disables colour under `--color auto`. |

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0`  | Success. |
| `1`  | Resolution failed, lockfile cannot be written (missing hash), download failed, missing `[project].dependencies`, a `--build-requirements` run whose project declares no `[build-system]`, invalid `[tool.nab]` configuration, or `--locked` found the lockfile out of date or missing. |
| `130` | Interrupted with Ctrl-C. `nab` prints `error: interrupted` and exits. |

[PEP 751]: https://peps.python.org/pep-0751/
