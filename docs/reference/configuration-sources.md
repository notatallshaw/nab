<a name="layered-configuration-sources"></a>

# Configuration sources

A setting's scope determines where Nab reads it:

| Scope   | Settings                                                                | Accepted sources                                                 |
| ------- | ----------------------------------------------------------------------- | ---------------------------------------------------------------- |
| Project | Resolve settings, such as `dist-policy`, `metadata-policy` and `matrix` | Project files and available `--project-*` flags                  |
| User    | `offline`, `cache-dir`, `http-backend`, `max-concurrency`               | System, user or project `nab.toml`; `NAB_*` variables; CLI flags |

`pyproject.toml` accepts project settings under `[tool.nab]`. Standalone `nab.toml` files use keys
at the top level. For example, a user file can set machine-specific defaults:

```toml
# ~/.config/nab/nab.toml
offline = true
cache-dir = "/fast/disk/nab-cache"
```

A `nab.toml` beside `pyproject.toml` can set project options too:

```toml
constraints = ["urllib3<2"]

[[indexes]]
name = "internal"
url = "https://pypi.example.com/simple/"
```

## Precedence

For sources allowed by a setting's scope, precedence increases from left to right:

```text
default -> system file -> user file -> project files -> environment -> CLI
```

Nab uses the whole value from the highest-precedence source. Lists and tables are replaced, not
merged. Passing `--project-constraint` twice replaces the file's constraints with those two values.

The project files, `[tool.nab]` and the adjacent `nab.toml`, share one rank. A key set in both must
have identical whole values. Different lists or different subkeys of an `environment` table are
errors naming both files.

## File locations

Nab reads the system file at `/etc/nab/nab.toml`. The user file is `$XDG_CONFIG_HOME/nab/nab.toml`,
or `~/.config/nab/nab.toml` when that variable is unset or empty.

Project-directory discovery uses the directory containing `pyproject.toml`; it does not search
parent directories.

## Environment variables

Only user settings have `NAB_*` bindings:

| Variable              | Option            | Value                          |
| --------------------- | ----------------- | ------------------------------ |
| `NAB_OFFLINE`         | `offline`         | `1`, `0`, `true` or `false`    |
| `NAB_CACHE_DIR`       | `cache-dir`       | Cache path                     |
| `NAB_HTTP_BACKEND`    | `http-backend`    | `urllib3`, `httpx` or `httpx2` |
| `NAB_MAX_CONCURRENCY` | `max-concurrency` | Positive integer               |

Unknown `NAB_*` names produce a warning. `-qq` and `NAB_VERBOSITY=silent` suppress it.
`NAB_VERBOSITY` and `NAB_NO_PROGRESS` are [output settings](cli.md).

## CLI overrides

Use `--project-<key>` for scalar or list project options. Structured tables are file-only, except
for the `--project-matrix-*` and `--project-environment-*` flags. Each changes one table key and
keeps the other file-supplied keys.

An override prints a notice and is recorded in the lock. The [CLI reference](cli.md) lists the
flags; [Selecting what to lock](selection.md) covers groups, extras and workspaces.

`--project-dist-policy` replaces the whole `dist-policy` value, including resetting
`trust-unverified-deps`. Use the file's table form to keep that flag. `--project-metadata-policy`
changes metadata selection independently.

## Inspecting settings

```bash
nab config list
nab config explain dist-policy
```

`nab config` reports values and their sources. It infers an omitted `mode` from the merged matrix
and labels it `inferred from matrix`.

`lock` and `download` also apply command-specific defaults: declared platforms default to
`build-policy = "never"`, and discovered workspace members become local sources.
