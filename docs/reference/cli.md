# CLI

`nab` exposes four subcommands: `lock`, `download`, `config`, and
`cache`. The first two read project shape from `[tool.nab]` in the
project's `pyproject.toml` or a project-directory `nab.toml`, and their
flags shape the resolve as well as the run; `--project-<key>` overrides
a project key for one run. `config` inspects the layered configuration,
and `cache` inspects and clears the on-disk cache.

## Synopsis

```
nab [GLOBAL FLAGS] lock     [PATH] [RUNTIME OPTIONS] [--output PATH] [--format pylock|requirements|requirements-without-hashes]
nab [GLOBAL FLAGS] download [PATH] [RUNTIME OPTIONS] [--output DIR] [--max-concurrency N]
nab [GLOBAL FLAGS] config   {list | get <key> | explain <key>} [--path PATH]
nab [GLOBAL FLAGS] cache    {dir | verify | clear} [--cache-dir PATH]
nab [COMMAND] --version | -V
nab [COMMAND] --help | -h
```

`GLOBAL FLAGS` are the verbosity, colour, and progress knobs listed under
Global flags below. They are rows of the same table a command's own flags
come from, so they work on either side of the command name.

`--` ends the options for the level it appears at: `nab lock -- --upgrade`
passes `--upgrade` as the project path, while `nab -- lock` still runs
`lock`.

`PATH` is positional and defaults to `pyproject.toml` in the
current directory. Run `nab lock --help` (or `-h`) for the full
per-command flag list. Boolean flags render as a `--flag` /
`--no-flag` pair (for example `--cache` / `--no-cache`). `--offline`
is layered, so an explicit `--offline True` / `--offline False`
overrides the config layers; bare `--offline` / `--no-offline` are
shorthands for those two values.

## `nab lock`

Resolve and emit a lockfile or pin list.

* What a run selects, and when a selection forks it: `--groups`,
  `--all-groups`, `--extras`, `--all-extras`, `--build-requirements`,
  and the workspace flags `--workspace-discovery` and
  `--no-emit-workspace`. See [Selecting what to lock](selection.md), and
  [Lock a workspace](../how-to/workspaces.md) for declaring one.
* What each format writes, where it is written, and what universal mode
  changes: `--format` and `--output`. See [Output formats](formats.md).
* What a failed resolve prints, and what `-v` adds to it. See
  [Resolution failures](diagnostics.md).

### Resolving for another Python

`--python X.Y` resolves for that Python on this machine instead of the
running interpreter, like pip's `--python-version`; it moves only the
python axis, so a declared `[tool.nab.environment].platform` stays. It is
rejected in universal mode, where the matrix declares the Python axis.

### Project overrides

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
reproducibility notice on stderr, which `-q` drops, and records the
override in the lockfile's `[tool.nab]` block, since the lock no longer
derives from the committed files alone.

### Checking and refreshing a lock

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
  they do on `nab lock` (see [Selecting what to lock](selection.md)), so
  they decide which artefacts are downloaded.
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

A failed resolve prints the same message and `Diagnostics:` section as
`nab lock`; see [Resolution failures](diagnostics.md).

A summary of how many files were written and how many were
already present is printed to stderr.

## `nab config`

Inspect the effective layered configuration (read-only). Three
actions:

* `nab config list` prints every option with its value, scope, and
  the source it came from.
* `nab config get <key>` prints one effective value.
* `nab config explain <key>` prints a header naming the key, its scope
  and its type, then the option's own help line and the page in `docs/`
  that documents it, then the full source stack. The winning row carries
  a `>` gutter and the status `winner`, and every source it beats is
  `shadowed`. A shadowed source contributes nothing to the value,
  whatever the key's type.

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
* `nab cache verify` walks the cached index records read-only and lists
  any corrupt entry on stdout by path and reason, exiting 1 when it found
  one. Cloned repositories and extracted archives hold upstream files, so
  they are not parsed.
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

Every command takes these, on either side of its name, and each one is a
row of the same table the command's own flags come from.

<!-- generated by tasks/gen_cli.py --write from nab/optiontable.py; do not edit -->
| Flag | Effect |
| ---- | ------ |
| `-v`, `--verbose` | raise verbosity; -v adds INFO records, -vv adds DEBUG |
| `-q`, `--quiet` | lower verbosity; -q drops the summary and notes, -qq keeps errors alone |
| `--color` | when to colour nab's output |
| `--no-color` | shorthand for --color never |
| `--no-progress` | suppress the live progress line |
| `-V`, `--version` | print the version and exit |
| `-h`, `--help` | print this help and exit |
<!-- /generated -->

`--version` and `--help` end the line where they stand: `nab lock --help`
prints the `lock` page and `nab lock --version` prints the version. Neither
loads a command module. The verbosity, colour, and progress flags are
described under Output control below.

## Output control

These flags set how much `nab` writes to stderr, whether it colours it,
and whether it animates a progress line. They work with `lock`, `download`,
`config`, and `cache`. stdout carries only the requested output (the
lockfile, the requirements list, the `config` dump), so it stays pipeable
at every verbosity.

| Flag | Effect |
| ---- | ------ |
| `-v`, `-vv`, `--verbose` | Raise verbosity. `-v` adds the engine's `INFO` records and deepens the `Diagnostics:` section of a resolution failure, `-vv` adds `DEBUG`. `--verbose` counts as one `-v`; repeats add, and `-vvv` saturates at `-vv`. |
| `-q`, `-qq`, `--quiet` | Lower verbosity. `-q` drops the run summary and notes, keeping warnings and errors; `-qq` keeps only errors. `--quiet` counts as one `-q`. |
| `--color` | When to colour nab's output: `auto` (default), `always`, or `never`. `auto` asks each stream on its own, so a help page piped to a file is plain while a refusal on the terminal beside it is not, and it honours `NO_COLOR`, `FORCE_COLOR`, and `TERM=dumb`; `always` and `never` win outright. Colour marks a message's leading token, a heading, and a spelling you can type, so every page reads the same stripped. |
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
| `NAB_VERBOSITY` | Default verbosity when no `-v` / `-q` flag is given: one of `silent`, `quiet`, `normal`, `verbose`, `debug`. A `-v` / `-q` flag overrides it. An unrecognised value is rejected by any command that reads it; `--version` and `--help` do not read it, so they neither honour nor refuse it. |
| `NAB_NO_PROGRESS` | If set to a non-empty value, suppress the live progress line, like `--no-progress`. |
| `NO_COLOR` | If set to a non-empty value, disable colour under `--color auto`. |
| `FORCE_COLOR` | If set to a non-empty value, force colour under `--color auto`. |
| `TERM` | `dumb` disables colour under `--color auto`. |

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0`  | Success. |
| `1`  | Resolution failed, lockfile cannot be written (a missing hash, or text that is not valid UTF-8), download failed, missing `[project].dependencies`, a `--build-requirements` run whose project declares no `[build-system]`, invalid `[tool.nab]` configuration, or `--locked` found the lockfile out of date or missing. |
| `2`  | Bad usage: an unrecognised flag or subcommand, or a malformed `--color` value or `NAB_VERBOSITY`. |
| `120` | Output was lost: writing to stdout or stderr failed, `nab` wrote to one that was closed before it started, or flushing one of them at exit failed. |
| `130` | Interrupted with Ctrl-C. `nab` prints `error: interrupted` and exits. |
