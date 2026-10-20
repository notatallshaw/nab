# Resolution settings

Set these keys under `[tool.nab]` in `pyproject.toml`, or at the top level of a project-directory
`nab.toml`. See [Configuration sources](configuration-sources.md) for precedence and CLI overrides.

| Key                 | Default                             | Meaning                                                                                                                                                                              |
| ------------------- | ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `mode`              | `"specific"`                        | Resolve one environment. `"universal"` resolves a [target matrix](target-matrix.md); its multi-target lock format is experimental.                                                   |
| `resolution`        | `"highest"`                         | Prefer the newest allowed version. `"lowest"` prefers the oldest; `"lowest-direct"` prefers the oldest for direct dependencies and the newest for transitive dependencies.           |
| `constraints`       | `[]`                                | Bound versions without adding packages to the resolve. Entries accept a package name, optional version specifier and optional marker; extras and direct-reference URLs are rejected. |
| `requires-python`   | `[project].requires-python`, if set | Declare the supported Python range as a PEP 440 specifier, such as `">=3.10"`. It is checked against the target and recorded in the lock; it does not select the target.             |
| `uploaded-prior-to` | unset                               | Exclude distributions uploaded after a timestamp. Accepts an ISO 8601 string or TOML datetime with an explicit timezone, or a `P<n>D` duration relative to the resolve anchor.       |
| `decision-order`    | `"arrival"`                         | Choose whether response timing can affect package decision order; see below.                                                                                                         |

```toml
[tool.nab]
resolution = "lowest-direct"
constraints = ["urllib3<2"]
requires-python = ">=3.10"
uploaded-prior-to = "2026-05-01T00:00:00Z"
decision-order = "stable"
```

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

| Value               | Behaviour                                                  |
| ------------------- | ---------------------------------------------------------- |
| `arrival` (default) | Rank a package on the listings that have already landed.   |
| `stable`            | Wait for the listing, then rank on its real version count. |

Use `stable` with `nab lock --locked` to keep fetch timing from changing the result. The index
contents and other inputs must also stay fixed.

Waiting for listings can make a resolve slower. Fetches still run concurrently.

Both settings use dependency look-ahead: for a package without extras, nab can skip a candidate
whose dependencies already contradict a root requirement or a selected version. Another resolver may
choose different versions that also satisfy the requirements.
