(layered-configuration-sources)=

# Configuration sources

nab separates project settings from settings for the machine running it. Each setting's scope
determines which files, environment variables and flags nab reads:

- Project-scope options describe the resolve itself, so they live with the project. Every
  `[tool.nab]` key is project-scope (`mode`, `indexes`, `constraints`, `vcs`, `workspace`,
  `dist-policy`, `build-policy`, `environment`, `conflicts`, `matrix`, `packages`, `resolution`, and
  the rest), and each may be set in either `pyproject.toml`'s `[tool.nab]` or a project-directory
  `nab.toml`.

  They are never read from a user/system file or an environment variable. Each project option with a
  scalar or list form also takes a CLI override under a `--project-` prefix (for example
  `--project-resolution`); the structured table options stay file-only, except `matrix` and
  `environment`, which the `--project-<table>-<key>` flags set one key at a time.

- User-scope options (`offline`, `cache-dir`, `http-backend`, `max-concurrency`) describe how a run
  executes on this machine, so they may come from a system, user, or project `nab.toml`, a `NAB_*`
  environment variable, or the CLI. They are rejected in `pyproject.toml`'s `[tool.nab]`, which is
  project-scope only.

## Precedence

Sources are consulted low to high; a higher source wins:

1. built-in default
2. system `nab.toml` (`/etc/nab/nab.toml`)
3. user `nab.toml` (`$XDG_CONFIG_HOME/nab/nab.toml`, else `~/.config/nab/nab.toml`)
4. `pyproject.toml` `[tool.nab]` and the project-directory `nab.toml` (same precedence; setting one
   key in both files is covered below)
5. `NAB_*` environment variables
6. the CLI flag

### Replacing values

nab uses the complete value from the highest-precedence source that sets a key. It does not merge
that value with lower-precedence sources.

A CLI `constraints` list replaces every lower-precedence constraint. An `[environment]` table
replaces every lower-precedence environment. No source adds to the one beneath it; to extend a list,
edit the file that declares it. A `--project-<table>-<key>` flag replaces only its named key in the
file's table.

The two project files share one rank. They may set the same key only to identical whole values; a
difference is an error naming both files. This applies equally to lists, arrays of tables, and an
`environment` table whose files set different subkeys.

## File formats and locations

The standalone `nab.toml` files use the same key names as `[tool.nab]`, but at the top level (no
`[tool.nab]` table):

```toml
# ~/.config/nab/nab.toml
offline = true
cache-dir = "/fast/disk/nab-cache"
```

A project-directory `nab.toml` may set any project-scope key the same way, so it can drive the
resolve without touching `pyproject.toml`:

```toml
# nab.toml next to pyproject.toml
constraints = ["urllib3<2"]

[[indexes]]
name = "internal"
url = "https://pypi.example.com/simple/"
```

Project-directory discovery is the directory of the `pyproject.toml` only; there is no walk-up.

`nab config` reports values before resolve-time derivation. Universal mode defaults `build-policy`
to `never` and rejects an explicit non-`never` setting. Workspace discovery adds members to
`local-sources` only during a resolve.

## Environment variables

| Variable              | Option            | Effect                                                                          |
| --------------------- | ----------------- | ------------------------------------------------------------------------------- |
| `NAB_OFFLINE`         | `offline`         | `1`/`0`/`true`/`false`.                                                         |
| `NAB_CACHE_DIR`       | `cache-dir`       | Cache root path.                                                                |
| `NAB_HTTP_BACKEND`    | `http-backend`    | `urllib3`, `httpx`, or `httpx2`.                                                |
| `NAB_MAX_CONCURRENCY` | `max-concurrency` | Parallel HTTP fetches, for the resolve as well as the downloads (at least `1`). |

An unknown `NAB_*` name is ignored with a warning; `-qq` and `NAB_VERBOSITY=silent` suppress it.
`NAB_VERBOSITY` and `NAB_NO_PROGRESS` belong to the [output layer](cli.md) and pass through. Run
`nab config list` to see each effective value and source.

(cli-overrides)=

## CLI overrides

The [CLI reference](cli.md) lists every flag. See [Selecting what to lock](selection.md) for groups,
extras, and workspaces, and [Output formats](formats.md) for `--format` and `--output`.

`--project-<key>` overrides a scalar or list project option for one run. The matrix and environment
flags replace one key in the file's table, leaving its other keys in place. Passing
`--project-constraint` twice replaces the file's entire `constraints` list with those two values. An
override prints a notice and is recorded in the lockfile.

`--project-dist-policy` takes a bare policy, so it replaces the whole `dist-policy` value and resets
`trust-unverified-deps`; set the table form in a file to keep that flag.
