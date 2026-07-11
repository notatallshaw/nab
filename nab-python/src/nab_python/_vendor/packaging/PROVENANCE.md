# Vendored `packaging` snapshot

This directory is a copy of the `packaging` library, vendored so nab can use
the public `VersionRange` API before a `packaging` release containing it is
published to PyPI. The tree is pristine `pypa/packaging` at a pinned commit
plus at most one checked-in patch, and nothing else.

## Model

- `tasks/vendoring/packaging.pin` holds the upstream commit the tree is pinned
  to.
- `tasks/vendoring/packaging.patch`, when present, is the only divergence from
  pristine; the rebuild reapplies it after refreshing. With no patch file the
  tree is byte-identical to upstream at the pin.
- `tasks/vendor-packaging.sh` fetches `pypa/packaging` at the pin, replaces this
  package with the pristine `src/packaging/` tree plus the repo-root license
  texts, and reapplies the patch. `--check` rebuilds into a temp location and
  fails if the committed tree has drifted. This file is nab's own and stays out
  of both the rebuild and the comparison.
- CI runs `tasks/vendor-packaging.sh --check`, so the committed tree is proven
  to equal pristine-at-pin plus the patch on every push.

## Refreshing

1. Bump `tasks/vendoring/packaging.pin` to the new upstream commit.
2. If the patch no longer applies, rebase its source branch onto the new pin
   and regenerate `packaging.patch` from it.
3. Run `tasks/vendor-packaging.sh`, then the test suite.

## License

`packaging` is dual-licensed under the Apache License 2.0 and the 2-Clause BSD
License. The LICENSE files here are the upstream texts; nothing is relicensed.

## Why vendor instead of depending on it

`packaging` now ships a public `VersionRange` class with set algebra
(intersection, union, complement, difference), the `is_subset` / `is_superset`
/ `is_disjoint` predicates, `is_empty`, `filter`, and a `SpecifierSet.to_range()`
factory that nab's PubGrub solver depends on. The class landed in `main` via
https://github.com/pypa/packaging/pull/1267 and the difference operator plus the
relation predicates via https://github.com/pypa/packaging/pull/1298. None of
this has appeared in a PyPI release yet, so there is no version to pin in
`pyproject.toml`. Vendoring is a temporary measure.

## Removal plan

Delete this directory and reinstate `packaging` as a normal dependency once a
release containing the merged `VersionRange` class reaches PyPI:

- Add `packaging>=<release>` back to `nab-python/pyproject.toml`
  `[project].dependencies`.
- Search-replace `from nab_python._vendor.packaging` -> `from packaging` and
  `import nab_python._vendor.packaging` -> `import packaging` across the
  workspace.
- Remove this `_vendor/` tree and the `tasks/vendoring/` files.
