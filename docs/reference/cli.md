# CLI

nab resolves Python dependencies, writes locks, and downloads artifacts. It does not install
packages.

## Usage

```text
nab lock [OPTIONS] [PATH]
nab download [OPTIONS] [PATH]
nab config [OPTIONS] {list|get|explain} [KEY]
nab cache [OPTIONS] {dir|verify|clear}
```

`PATH` defaults to `pyproject.toml`. `config get` and `config explain` require a `KEY`. Run
`nab COMMAND --help` for the full option list. Global flags work before or after the command name.
Use `--` before a path beginning with `-`.

## `nab lock`

Resolve dependencies and write a lockfile or requirements file.

### `--format FORMAT`

Choose `pylock` (default), `requirements`, or `requirements-without-hashes`. See
[Output formats](formats.md) for what each includes.

### `--output PATH`

Write to `PATH`, or stdout with `-`. Single-environment defaults are `pylock.toml` or
`requirements.txt`. Universal requirements go to stdout unless you provide an
{ref}`output template <output>`.

### `--locked`

Check that the existing lock is current without writing it. Exit `1` if it is missing or out of
date. Requires single-environment pylock output to a file. See
{ref}`Checking a lock <checking-the-lock-in-ci>`.

### `--upgrade`

Reset a relative upload cutoff (`P<n>D`) to the current time instead of reusing the existing lock's
timestamp.

### `--build-requirements`

Lock `[build-system].requires` instead of project dependencies. Defaults to `pylock.build.toml` or
`build-requirements.txt`. Cannot be combined with group or extra selection. See
{ref}`Build requirements <build-requirements>`.

### `--no-emit-workspace`

Omit workspace members from the output while still using them during resolution. Install those
members separately. See {ref}`Workspace flags <workspace-flags>`.

## `nab download`

Resolve dependencies again and download their artifacts. This does not read an existing lock. Files
with matching hashes are kept; local and VCS packages are skipped.

### `--output DIR`

Download into `DIR`. Default: `wheels/`. Multi-target resolves download every target's artifacts.

### `--max-concurrency N`

Allow up to `N` simultaneous HTTP fetches. Default: `8`; minimum: `1`. Also available to `config`.
Environment variable: `NAB_MAX_CONCURRENCY`.

## Selection options

These options apply to `lock` and `download`. See [Selecting what to lock](selection.md) for
conflicts and workspaces.

### `--python VERSION`

Resolve for this Python version instead of the running interpreter. Does not change a configured
platform. Cannot be combined with universal mode or `--project-environment-python`.

### `--groups NAME...`, `--all-groups`

Include named dependency groups, or every declared group.

### `--extras NAME...`, `--all-extras`

Include named project extras, or every declared extra.

### `--workspace-discovery`, `--no-workspace-discovery`

Find and use workspace members during resolution. Enabled by default.

## Cache and network options

`lock`, `download`, and `config` accept these settings; `cache` also accepts `--cache-dir`.
Configuration files and environment variables can supply their defaults.

### `--cache-dir PATH`

Use this cache directory. Default: `$XDG_CACHE_HOME/nab` or `~/.cache/nab`. Environment variable:
`NAB_CACHE_DIR`. See [Caching](cache.md).

### `--offline [True|False|None]`, `--no-offline`

Disable network access and use cached or local data only. Use `--no-offline` or `--offline False` to
override an offline setting. Environment variable: `NAB_OFFLINE`.

### `--http-backend {urllib3,httpx,httpx2}`

Select the HTTP transport. Default: `urllib3`. The other backends require their
[installation extras](../how-to/install.md). Environment variable: `NAB_HTTP_BACKEND`.

### `--cache`, `--no-cache`

Enable or disable cache reads and writes for `lock` and `download`. Enabled by default. With
`--no-cache --offline`, all inputs must be local and builds must need no installation step.

## Project overrides

These flags override `[tool.nab]` settings for one `lock`, `download`, or `config` invocation.
Scalar and list flags replace the configured value; matrix and environment flags replace only their
named key. See {ref}`Configuration <cli-overrides>` for accepted values and examples.

- `--project-resolution`: prefer highest, lowest, or lowest-direct versions.
- `--project-decision-order`: use arrival or stable decision order.
- `--project-mode`: resolve one environment (`specific`) or a matrix (`universal`).
- `--project-requires-python`: set the supported Python version range.
- `--project-uploaded-prior-to`: exclude distributions uploaded after a timestamp or relative
  cutoff.
- `--project-dist-policy`: select allowed distribution types.
- `--project-build-policy`: select which source distributions may be built.
- `--project-build-requires-depth`: limit nested build environments.
- `--project-constraint`: constrain a package's versions. Repeat to replace the configured
  constraint list.
- `--project-default-group`: select a group on every resolve. Repeat for multiple groups.
- `--project-base-group`: assign a group name to project dependencies.
- `--project-build-group`: assign a group name to build requirements.
- `--project-matrix-python`: set the matrix's Python version range.
- `--project-matrix-platforms`: set platform IDs and optional `KEY=VALUE` tag settings.
- `--project-matrix-implementations`: select interpreter implementations.
- `--project-matrix-python-order`: resolve Python versions in ascending or descending order.
- `--project-matrix-python-patches`: pin minor versions using `MINOR=FULL` pairs.
- `--project-environment-python`: set one target Python version.
- `--project-environment-platform`: set one target platform and optional tag settings.
- `--project-environment-implementation`: set one target interpreter implementation.

## `nab config`

Inspect effective settings and their sources without changing them.

- `list`: print every setting and its source.
- `get KEY`: print one value.
- `explain KEY`: show the value and the sources considered for it.

### `--path PATH`

Read configuration for this project file. Default: `pyproject.toml`.

### `--include-rejected`

Continue past unknown or disallowed settings. `list` shows all rejected sources; `explain` shows
those for its key. `get` still prints only the value. Use `list` to inspect unknown keys.

## `nab cache`

Inspect or clear the [on-disk cache](cache.md).

- `dir`: print the cache directory, even if it does not exist.
- `verify`: report corrupt cached records; exit `1` if any are found.
- `clear`: remove cached records, including cloned repositories and extracted archives.

## Global flags

<!-- generated by tasks/gen_cli.py --write from nab/_cli/definition/options.py; do not edit -->

- `-v`, `--verbose`: raise verbosity; -v adds INFO records, -vv adds DEBUG
- `-q`, `--quiet`: lower verbosity; -q drops the summary and notes, -qq keeps errors alone
- `--color`: when to colour nab's output
- `--no-color`: shorthand for --color never
- `--no-progress`: suppress the live progress line
- `-V`, `--version`: print the version and exit
- `-h`, `--help`: print this help and exit

<!-- /generated -->

## Output control

`--color` accepts `auto` (default), `always`, or `never`. `auto` decides separately for each stream.
Verbose and quiet flags combine as the number of `-v` flags minus `-q` flags. Progress appears on
stderr only at normal verbosity in a terminal:

```
⠹ Resolving... 12 fetched, 5 pinned
```

Requested output goes to stdout; diagnostics and status go to stderr. See
[Resolution failures](diagnostics.md) for verbose failure reports.

## Environment variables

- `NAB_VERBOSITY`: default level (`silent`, `quiet`, `normal`, `verbose`, or `debug`). Explicit `-v`
  or `-q` overrides it; help and version ignore it.
- `NAB_NO_PROGRESS`: a non-empty value disables progress.
- `NO_COLOR`: a non-empty value disables automatic colour.
- `FORCE_COLOR`: a non-empty value, including `0`, forces automatic colour unless `NO_COLOR` is set.
- `TERM=dumb`: disables automatic colour unless `FORCE_COLOR` is set.
- `XDG_CACHE_HOME`: default cache base directory; relative paths are ignored.
- `XDG_CONFIG_HOME`: default configuration base directory; relative paths are ignored.

See {ref}`Configuration sources <layered-configuration-sources>` for `nab.toml` locations and other
`NAB_*` variables.

## Exit codes

- Success: `0`.
- Failed command or lock check: `1`.
- Invalid usage or output settings: `2`.
- Lost output through writing, flushing, or a closed stream: `120`.
- Interrupted with Ctrl-C: `130`.
