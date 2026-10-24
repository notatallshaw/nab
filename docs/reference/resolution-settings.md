# Resolution settings

Set these keys under `[tool.nab]` in `pyproject.toml`, or at the top level of a project-directory
`nab.toml`. See [Configuration sources](configuration-sources.md) for precedence and CLI overrides.

```toml
[tool.nab]
resolution = "lowest-direct"
constraints = ["urllib3<2"]
requires-python = ">=3.10"
uploaded-prior-to = "2026-05-01T00:00:00Z"
decision-order = "stable"
```

## Mode

When `mode` is omitted, nab uses `"specific"` for one target and `"universal"` when the effective
[target matrix](target-matrix.md) contains multiple targets. A singleton matrix still selects its
declared environment. An explicit mode overrides this default. The multi-target lock format is
experimental.

## Version preference

`resolution` defaults to `"highest"`, preferring the newest allowed version. `"lowest"` prefers the
oldest; `"lowest-direct"` prefers the oldest for direct dependencies and the newest for transitive
dependencies.

## Constraints

`constraints` defaults to `[]`. A constraint bounds versions without adding a package to the
resolve. Entries accept a name, optional version specifier and optional marker; extras and
direct-reference URLs are rejected.

## Python support

`requires-python` falls back to `[project].requires-python`, if set. It takes a
[version specifier](https://packaging.python.org/en/latest/specifications/version-specifiers/), such
as `">=3.10"`. Nab checks it against the target and records it in the lock; it does not select the
target.

## Upload cutoff

`uploaded-prior-to` is unset by default. It excludes distributions uploaded after the cutoff. Set an
ISO 8601 string or TOML datetime with an explicit timezone, or a `P<n>D` duration relative to the
resolve anchor.

A cutoff excludes files without upload times, except local `file://` and find-links artifacts. An
HTML index may lack upload times; see {ref}`Index serialization <index-serialization>`.
[Reproducible locks](reproducibility.md) explains the anchor and the limits of a cutoff.

## Prereleases and yanked files

Nab follows the
[prerelease admission rules](https://packaging.python.org/en/latest/specifications/version-specifiers/#handling-of-pre-releases).
To admit a prerelease explicitly, include it in a requirement such as `lib>=2.0rc1`. Version
preference still follows `resolution`.

Yanked files have separate admission rules. An exact pin can allow one; a wildcard or range alone
cannot. See [Yanked releases](yanking.md).

## Decision order

nab fetches listings in the background. Response timing and cache warmth can affect which valid
solution the default search selects.

| Value               | Behaviour                                                               |
| ------------------- | ----------------------------------------------------------------------- |
| `arrival` (default) | Rank packages using the listings received so far.                       |
| `stable`            | Wait for a package's listing before using its version count to rank it. |

Use `stable` with `nab lock --locked` to keep fetch timing from changing the result. The index
contents and other inputs must also stay fixed.

Waiting for listings can make a resolve slower. Fetches still run concurrently.

Both settings use dependency look-ahead: for a package without extras, nab can skip a candidate
whose dependencies already contradict a root requirement or a selected version. Another resolver may
choose different versions that also satisfy the requirements.
