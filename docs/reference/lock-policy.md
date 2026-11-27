# Lock policies

`lock-policy` controls what `nab lock` does when its output file already exists.

- `resolve` (default) resolves from scratch using current package information. With the default
  highest-version preference, this can pick up newer releases.
- `prefer-existing` tries the locked versions first. Nab checks them against current index listings,
  dependency metadata and artifact rules. If the saved dependencies no longer fit, it resolves again
  with the old versions as preferences.
- `reuse` checks the saved solution against the current project inputs. If it still fits and its
  sources support reuse, nab leaves the file unchanged without consulting the index. Otherwise it
  falls back to `prefer-existing`.

For example, suppose your project requires `demo>=1` and the lock contains `demo==1.0`. When 2.0
appears on the index, `prefer-existing` checks 1.0 and keeps it if it still qualifies. If the index
later removes 1.0, `prefer-existing` finds another eligible version. With the same project inputs,
`reuse` can keep the lock at 1.0 even after the files disappear. It also skips checks for newly
yanked files and changed remote metadata.

## Set the policy

For one run, pass the policy on the command line:

```bash
nab lock --lock-policy prefer-existing
```

To use it for the project, put the setting at the top level of `nab.toml` beside `pyproject.toml`:

```toml
lock-policy = "prefer-existing"
```

You can also set a user or system default in `nab.toml`. The `NAB_LOCK_POLICY` environment variable
overrides file settings, and the command-line flag overrides both. See
[Configuration sources](configuration-sources.md).

## When nab needs to resolve

With no usable saved lock, all three policies resolve from scratch. A compatible change to a
dependency bound can still allow `reuse` to keep the entire file, including its timestamp. Reuse
supports project and script inputs with HTTP(S) index artifacts. File indexes, local dependencies,
workspace members, VCS sources and direct archives require live resolution.

Both `prefer-existing` and `reuse` require highest-version resolution and pylock output to a file.
Neither can be combined with `--locked`. Use
[`nab lock --locked`](reproducibility.md#checking-the-lock-in-ci) to fail when the saved lock needs
updating. The `reuse` policy also conflicts with `--reproduce`.

With `reuse`, `--upgrade` runs the `prefer-existing` checks so nab can record a new timestamp for a
relative upload cutoff. To update one package while preferring the other locked versions, use
[`--upgrade-package NAME`](cli.md#--upgrade-package-name).
