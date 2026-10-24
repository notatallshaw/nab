(reproducibility)=

# Reproducible locks

Repeatable locking needs a fixed index view and a fixed search order:

- `[tool.nab].uploaded-prior-to` excludes distributions uploaded after its timestamp.
- `[tool.nab].decision-order = "stable"` prevents response timing and cache warmth from steering
  package order. See [Resolution settings](resolution-settings.md) for its cost.

## Fixed upload cutoffs

```toml
[tool.nab]
uploaded-prior-to = "2026-10-01T00:00:00Z"
decision-order = "stable"
```

With the index response held fixed, both settings reproduce the pin set. An absolute cutoff also
becomes the UTC `created-at`, so identical inputs produce identical lock bytes. `--upgrade` replaces
that timestamp with the run time.

The cutoff does not freeze an index. A later yank changes the listing, a deleted file is
unavailable, and nab trusts reported upload times. Files with no upload time are excluded, except
local `file://` and find-links artifacts, which remain eligible.

A resolve mixing those sources with an index is only partly time-bounded.

## Relative upload cutoffs

```toml
[tool.nab]
uploaded-prior-to = "P7D"
```

A relative `P<n>D` cutoff is measured from `created-at`. Re-locking reuses the existing lock's
timestamp; `--upgrade` moves the window. A first lock, stdout, requirements output, or a pylock
without `created-at` anchors to the run time.

(checking-the-lock-in-ci)=

## Checking the lock in CI

`nab lock --locked` re-resolves project inputs and compares the result with the committed pylock
without writing. It applies to file output in single-environment mode. nab resolves independently of
the existing pins and ignores the lock's `[tool.nab]` record of inputs and timestamps when comparing
results.

```bash
nab lock --locked
```

Extras and groups are sorted in the lock. Reordering the same names, or using `--all-extras` or
`--all-groups` for that set, does not change the arrays.

Input mismatches that can be proved without resolving fail early. Otherwise the full resolve runs,
so `decision-order = "arrival"` can produce a different answer on a colder cache.

Use `decision-order = "stable"` for this check. See [Use a lock](../how-to/use-the-lock.md) for the
CI command and the [CLI reference](cli.md) for mismatch behavior.

See [Lockfiles](lockfile.md) for the output formats.
