# CLI

`nab` exposes three subcommands: `lock`, `download`, and `config`. The
first two read project shape from `[tool.nab]` in the project's
`pyproject.toml` or a project-directory `nab.toml`; the CLI carries
runtime knobs and can override a project key for one run with a
`--project-<key>` flag. `config` inspects the layered configuration.

## Synopsis

```
nab [--version | -V]
nab lock     [PATH] [RUNTIME OPTIONS] [--output PATH] [--format pylock|requirements|requirements-without-hashes]
nab download [PATH] [RUNTIME OPTIONS] [--output DIR] [--max-concurrency N]
nab config   {list | get <key> | explain <key>} [--path PATH]
```

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
  `dependency-groups` and `default-groups` arrays.
* `--all-groups` selects every group defined in the project.

Extras selection:

* `--extras foo bar` folds entries from the project's own
  `[project.optional-dependencies]` table into the resolve. The
  selected extra names land in the lockfile's top-level `extras`
  array.
* `--all-extras` selects every declared extra.

Both `--groups` and `--extras` produce a single union resolve;
the lockfile records the selection but does not emit per-package
`'X' in extras` or `'X' in dependency_groups` markers.

`--output` defaults to `pylock.toml` for `pylock` and
`requirements.txt` for the two requirements formats. Pass
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
variable that would separate them.

Exits non-zero on resolution failure; the message starts with
`Resolution failed:` followed by a derivation tree, and any
captured diagnostics are appended under a `Diagnostics:` section.

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

Failed tuples render as `# {label}: FAILED` followed by the
indented error and exit `1`.

`--python X.Y` resolves for that Python on this machine instead of the
running interpreter, like pip's `--python-version`; it moves only the
python axis, so a declared `[tool.nab.environment].platform` stays. It is
rejected in universal mode, where the matrix declares the Python axis.

A project option can be overridden for one run with a `--project-<key>`
flag: `--project-resolution`, `--project-mode`, `--project-requires-python`,
`--project-uploaded-prior-to`, `--project-dist-policy`,
`--project-build-policy`, and the repeatable `--project-constraint` and
`--project-default-group`. Each changes the resolved set, so passing one
prints a reproducibility notice on stderr and records the override in the
lockfile's `[tool.nab]` block, since the lock no longer derives from the
committed files alone. `--project-resolution` was previously named
`--resolution`.

`--locked` re-resolves and checks that the committed `pylock.toml` is
already up to date, writing nothing. It exits non-zero if the lock would
change or is missing, so CI can assert the lock is current. It covers
`pylock` output to a file in single-environment mode.

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

`--offline`, `--cache-dir`, `--http-backend`, `--max-concurrency` and the
`--project-*` overrides flow through the same layered config sources `nab
lock` uses, so a `NAB_*` variable or a system/user/project `nab.toml` is
read for `nab download` as for `nab lock`.

A summary of how many files were written and how many were
already present is printed to stderr.

## `nab config`

Inspect the effective layered configuration (read-only). Three
actions:

* `nab config list` prints every option with its value, scope, and
  the source it came from.
* `nab config get <key>` prints one effective value.
* `nab config explain <key>` prints the full source stack for one
  key, the winning source marked with a `>`. Array options concatenate
  every layer instead of one winning, so each source is shown as a
  contribution and the merged effective value is printed on a trailing
  `= effective:` line. Add `--include-rejected` to also list sources
  that tried to set the key but were rejected by the category gate.

The same per-option override flags the run commands accept (the
`--project-*` overrides and the user knobs `--offline`, `--cache-dir`,
`--http-backend`, `--max-concurrency`) layer a CLI value on top, so the
inspector reflects the same effective values a run would see.

See [Configuration](configuration.md) for the source ladder and the
`NAB_*` environment variables.

## Runtime flags

Both subcommands accept the same runtime knobs:

| Flag | Default | Effect |
| ---- | ------- | ------ |
| `--cache-dir PATH` | `~/.cache/nab` | Override the on-disk cache root. |
| `--no-cache` | off | Disable cache for this run. Combine with `--offline` only if the cache already has every file. |
| `--offline {True,False}` | unset | Use cache only, never hit the network. Layered: `--offline True` forces offline, `--offline False` forces network even over a lower `offline = true`. Bare `--offline` / `--no-offline` are shorthands for `True` / `False`. |
| `--http-backend {urllib3,httpx}` | `urllib3` | Pick the async transport for fetches. Layered, so it can also be set in an `nab.toml` or `NAB_HTTP_BACKEND`. `httpx` needs its extra (see [Install nab](../how-to/install.md)). |

`urllib3` is the only backend pulled in by the base install. The
others surface a helpful `ImportError` if selected without the
matching extra.

## Global flags

| Flag | Effect |
| ---- | ------ |
| `--version`, `-V` | Print `nab <version>` and exit `0`. |
| `--help`, `-h` | Standard argparse-style help. Per-subcommand help works too: `nab lock --help`. |

## Environment variables

| Variable | Effect |
| -------- | ------ |
| `XDG_CACHE_HOME` | If set, `nab`'s default cache root is `$XDG_CACHE_HOME/nab` instead of `~/.cache/nab`. |

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0`  | Success. |
| `1`  | Resolution failed, lockfile cannot be written (missing hash), download failed, missing `[project].dependencies`, invalid `[tool.nab]` configuration, or `--locked` found the lockfile out of date or missing. |
| `130` | Interrupted with Ctrl-C. `nab` prints `Aborted.` and exits. |

[PEP 751]: https://peps.python.org/pep-0751/
