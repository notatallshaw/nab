<a name="layered-configuration-sources"></a>

# Configuration sources

A setting's scope determines where Nab reads it:

| Scope   | Settings                                                                                       | Accepted sources                                                                                 |
| ------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Project | Project policy and target declarations, such as `dist-policy`, `metadata-policy` and `matrix`  | Project files, named `--project-*` flags and `--setting project.KEY=TOML`                        |
| User    | Transport/cache controls and workflow defaults, including format, selections and `lock-policy` | System, user or project `nab.toml`, `NAB_*` variables, named flags and `--setting user.KEY=TOML` |

`pyproject.toml` accepts project settings under `[tool.nab]`. Standalone `nab.toml` files use keys
at the top level. For example, a user file can set machine and workflow defaults:

```toml
# ~/.config/nab/nab.toml
offline = true
cache-dir = "/fast/disk/nab-cache"
lock-policy = "resolve"
```

With `--script`, project settings come from the script's inline `[tool.nab]` and a `nab.toml` beside
the script. See [Lock a Python script](../how-to/scripts.md) for the permitted settings.

A `nab.toml` beside the input can set project options too:

```toml
constraints = ["urllib3<2"]

[[indexes]]
name = "internal"
url = "https://pypi.example.com/simple/"
```

## Precedence

For sources allowed by a setting's scope, precedence increases from left to right:

```text
default -> system file -> user file -> project or script files -> environment -> CLI
```

Nab uses the whole value from the highest-precedence source. Lists and tables are replaced, not
merged. Passing `--project-constraint` twice replaces the file's constraints with those two values.

The input's `[tool.nab]` and adjacent `nab.toml` share one rank. A key set in both must have
identical whole values. Different lists or different subkeys of an `environment` table are errors
naming both files.

## File locations

Nab reads the system file at `/etc/nab/nab.toml`. The user file is `$XDG_CONFIG_HOME/nab/nab.toml`,
or `~/.config/nab/nab.toml` when that variable is unset or empty.

Directory-level `nab.toml` discovery uses the directory containing `pyproject.toml` or, with
`--script`, the script's directory. It does not search parent directories.

## Environment variables

Only user settings have `NAB_*` bindings:

| Variable              | Option            | Value                                   |
| --------------------- | ----------------- | --------------------------------------- |
| `NAB_OFFLINE`         | `offline`         | `1`, `0`, `true` or `false`             |
| `NAB_CACHE_DIR`       | `cache-dir`       | Cache path                              |
| `NAB_HTTP_BACKEND`    | `http-backend`    | `urllib3`, `httpx` or `httpx2`          |
| `NAB_LOCK_POLICY`     | `lock-policy`     | `resolve`, `prefer-existing` or `reuse` |
| `NAB_MAX_CONCURRENCY` | `max-concurrency` | Positive integer                        |

| `NAB_FORMAT` | `format` | `pylock`, `requirements` or `requirements-without-hashes` | |
`NAB_CACHE` | `cache` | `1`, `0`, `true` or `false` | | `NAB_GROUPS` | `groups` | TOML array, such
as `["dev"]` | | `NAB_ALL_GROUPS` | `all-groups` | `1`, `0`, `true` or `false` | | `NAB_EXTRAS` |
`extras` | TOML array, such as `["test"]` | | `NAB_ALL_EXTRAS` | `all-extras` | `1`, `0`, `true` or
`false` | | `NAB_WORKSPACE_DISCOVERY` | `workspace-discovery` | `1`, `0`, `true` or `false` | |
`NAB_BUILD_REQUIREMENTS` | `build-requirements` | `1`, `0`, `true` or `false` | |
`NAB_NO_EMIT_WORKSPACE` | `no-emit-workspace` | `1`, `0`, `true` or `false` |

Unknown `NAB_*` names produce a warning. `-qq` and `NAB_VERBOSITY=silent` suppress it.
`NAB_VERBOSITY` and `NAB_NO_PROGRESS` are [output settings](cli.md).

## CLI overrides

Every configuration key accepts a namespaced `--setting` assignment using its file value syntax:

```bash
nab lock --setting 'project.indexes=[{name="internal",url="https://pypi.example.com/simple/"}]'
nab lock --setting 'project.packages={example={dependencies=["requests>=2"]}}'
nab lock --setting 'user.lock-policy="reuse"'
```

`project` and `user` identify the setting's scope. Strings need TOML quotes; arrays and inline
tables use ordinary TOML syntax. Relative paths in assignments are relative to the working
directory. An assignment replaces the whole value; repeated assignments to the same key keep the
last value. Embedded URL credentials are omitted from recorded overrides; supply authentication
separately.

Named scalar/list flags remain available. The `--project-matrix-*` and `--project-environment-*`
families change one table key and retain the other file-supplied keys. A whole-value assignment
cannot be combined with a dedicated flag for that same key.

A project-scope override prints a notice and is recorded in the lock. The [CLI reference](cli.md)
lists the flags; [Selecting what to lock](selection.md) covers groups, extras and workspaces.

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

## Settings and command actions

Persistent settings share file, environment and CLI handling. Input paths, output destinations and
one-run actions such as `--help`, `--locked`, `--reproduce` and `--upgrade` select a command or its
operation for that invocation. Output preferences retain their standard environment forms.
