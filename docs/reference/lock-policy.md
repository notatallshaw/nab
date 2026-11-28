# Lock policies

Suppose your project requires `demo>=1`. You locked version 1.0 last week, and 2.0 is now on the
index. The next `nab lock` can keep 1.0 or pick up 2.0.

- `resolve` (the default) starts from scratch. With the default settings, nab picks 2.0 if it meets
  your requirements.
- `prefer-existing` checks whether 1.0 is still on the index and whether its files and dependencies
  still meet your requirements. If so, nab keeps 1.0. If that release has been removed, nab looks
  for another version while still preferring the locked versions of other packages.
- `reuse` checks the lockfile against your project. If the lock still works and its packages come
  from a supported index, nab leaves it alone. Nab won't see if the index has removed or yanked 1.0
  or changed its metadata. If nab can't reuse the file, it falls back to `prefer-existing`.

Without a usable lockfile, all three policies start from scratch.

## Set the policy

For a single run, use `nab lock --lock-policy prefer-existing`. To make that the project default,
add this to `nab.toml` beside `pyproject.toml`:

```toml
lock-policy = "prefer-existing"
```

You can also set the policy in your user or system `nab.toml`. The `NAB_LOCK_POLICY` environment
variable overrides the file setting, and the command-line flag overrides both. See
[Configuration sources](configuration-sources.md).

## When can nab reuse the lockfile?

You can change a version bound and still reuse the file. For example, narrowing `demo>=1` to
`demo>=1,<2` still allows the locked version, 1.0. Reusing the lockfile preserves its timestamp.

Nab can reuse locks for projects and scripts whose packages come from HTTP or HTTPS indexes. File
indexes, local dependencies, workspace members, VCS sources and direct archives all require a new
resolve.

## Related options

Both `prefer-existing` and `reuse` require `resolution = "highest"` and a saved pylock, uv, Poetry
or PDM file. To check the lockfile in CI, run
[`nab lock --locked`](reproducibility.md#checking-the-lock-in-ci) with the default `resolve` policy.
You can use `--reproduce` with `resolve` or `prefer-existing`.

Use [`--upgrade-package NAME`](cli.md#--upgrade-package-name) to update one package while keeping
other locked versions where possible. With `reuse`, `--upgrade` switches to `prefer-existing` for
that run and resets the timestamp for a relative upload cutoff.
