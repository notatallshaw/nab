<a name="reproducibility"></a>

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

## Checking the lock in CI

`nab lock --locked` checks current project inputs against the saved Pylock selections. Compact input
fingerprints provide an unchanged-input fast path. New releases and index availability do not affect
this check. See [Use a lock](../how-to/use-the-lock.md) for CI setup and limitations.

To check whether a fresh resolve still produces the same result, use:

```bash
nab lock --reproduce
```

This can contact indexes and select newer releases. Use `decision-order = "stable"` to avoid
response timing changing the answer. Comparison ignores `[tool.nab]` data and writes nothing.

See [Lockfiles](lockfile.md) for the output formats.
