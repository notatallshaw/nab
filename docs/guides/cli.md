# CLI

`nab` exposes two subcommands: `lock` and `download`. Both read
project shape from `[tool.nab]` in the project's `pyproject.toml`;
the CLI itself carries only runtime knobs.

## Synopsis

```
nab [--version | -V]
nab lock     [PATH] [RUNTIME OPTIONS] [--output PATH] [--format pylock|requirements|requirements-without-hashes]
nab download [PATH] [RUNTIME OPTIONS] [--output DIR] [--max-concurrency N]
```

`PATH` is positional and defaults to `pyproject.toml` in the
current directory. Run `nab lock --help` (or `-h`) for the full
per-command flag list. Boolean flags render as a `--flag` /
`--no-flag` pair (for example `--cache` / `--no-cache`).

## `nab lock`

Resolve and emit a lockfile or pin list. Three formats:

* `--format pylock` (default) writes a [PEP 751] `pylock.toml`.
* `--format requirements` writes a pip-compatible
  `requirements.txt` with one `--hash=sha256:...` line per
  recorded artefact.
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
`--output -` to write to stdout instead.

Exits non-zero on resolution failure; the message starts with
`Resolution failed:` followed by a derivation tree, and any
captured diagnostics are appended under a `Diagnostics:` section.

Universal mode (`[tool.nab].mode = "universal"`) is supported for
all three formats:

* `pylock` produces one PEP 751 file with per-tuple `Package`
  entries gated by markers (`python_version`, `sys_platform`,
  `platform_machine`). Versions agreed across every tuple appear
  once without a marker; divergent versions appear once per
  `(version, source)` group with the matching tuples disjoined.
* `requirements` and `requirements-without-hashes` emit a
  sequence of `# label` comment blocks, one per
  `(python, platform)` tuple, followed by that tuple's pins. Pip's
  hash-checking mode cannot install a single requirements.txt
  across multiple tuples, so the per-tuple block format is for
  inspection or for tools that consume one block at a time.

Failed tuples render as `# {label}: FAILED` followed by the
indented error and exit `1`.

## `nab download`

Resolve and download every wheel and sdist into a local
directory. The download is idempotent: files whose recorded
sha256 matches a local file are left alone. Local and VCS pins
are skipped. Single-environment only.

* `--output` defaults to `wheels/`.
* `--max-concurrency` controls parallel HTTP fetches (default `8`).

A summary of how many files were written and how many were
already present is printed to stderr.

## Runtime flags

Both subcommands accept the same runtime knobs:

| Flag | Default | Effect |
| ---- | ------- | ------ |
| `--cache-dir PATH` | `~/.cache/nab` | Override the on-disk cache root. |
| `--no-cache` | off | Disable cache for this run. Combine with `--offline` only if the cache already has every file. |
| `--offline` | off | Use cache only, never hit the network. |
| `--http-backend {urllib3,httpx}` | `urllib3` | Pick the async transport for fetches. `httpx` needs its extra (see [installation](installation.md)). |

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
| `1`  | Resolution failed, lockfile cannot be written (missing hash), download failed, missing `[project].dependencies`, or invalid `[tool.nab]` configuration. |
| `130` | Interrupted with Ctrl-C. `nab` prints `Aborted.` and exits. |

[PEP 751]: https://peps.python.org/pep-0751/
