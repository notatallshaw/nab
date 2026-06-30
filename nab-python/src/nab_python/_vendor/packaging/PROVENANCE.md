# Vendored `packaging` snapshot

This directory holds an unmodified copy of the `packaging` library taken
from `pypa/packaging:main`, vendored so nab can use the public
`VersionRange` API before a `packaging` release containing it is
published to PyPI.

## Source

- Upstream repository: https://github.com/pypa/packaging
- Source branch: `pypa/packaging:main`
- Pinned commit: `c7d859d1333887226f521f59b1304257c04eeca2`
- Snapshot date: 2026-06-30

The snapshot is the full `src/packaging/` tree at that commit, plus
`LICENSE`, `LICENSE.APACHE`, and `LICENSE.BSD` from the repository
root. No code changes were made; relative imports inside `packaging`
(`from .version import Version`, etc.) keep working when the package
is loaded as `nab_python._vendor.packaging`.

## License

`packaging` is dual-licensed under the Apache License 2.0 and the
2-Clause BSD License. The LICENSE files in this directory are the
upstream texts; nothing here is relicensed.

## Why vendor instead of depending on it

`packaging` now ships a public `VersionRange` class with set algebra
(intersection, union, complement, difference), the `is_subset` /
`is_superset` / `is_disjoint` relation predicates, `is_empty`, `filter`,
and a `SpecifierSet.to_range()` factory that nab's PubGrub solver depends
on. The class landed in `main` via
https://github.com/pypa/packaging/pull/1267, and the difference operator
(`-`) plus the relation predicates via
https://github.com/pypa/packaging/pull/1298. None of this has appeared in
a PyPI release yet, so there is no version we can pin in `pyproject.toml`.
Vendoring is a temporary measure.

## Removal plan

Delete this entire directory and reinstate `packaging` as a normal
dependency once a `packaging` release containing the merged
`VersionRange` class has been published to PyPI.

Once that holds:

- Add `packaging>=<release>` back to `nab-python/pyproject.toml`
  `[project].dependencies`.
- Search-replace `from nab_python._vendor.packaging` ->
  `from packaging` and `import nab_python._vendor.packaging` ->
  `import packaging` across the workspace.
- Remove this `_vendor/` tree.

## Updating the snapshot

To refresh against a newer `pypa/packaging:main`:

```
cd packaging  # the upstream fork checkout
git fetch upstream main
NEW_SHA=$(git rev-parse upstream/main)
DEST=/path/to/nab/nab-python/src/nab_python/_vendor/packaging
for f in $(git ls-tree -r --name-only upstream/main -- src/packaging); do
    rel=${f#src/packaging/}
    mkdir -p "$(dirname "$DEST/$rel")"
    git show "upstream/main:$f" > "$DEST/$rel"
done
for f in LICENSE LICENSE.APACHE LICENSE.BSD; do
    git show "upstream/main:$f" > "$DEST/$f"
done
```

Then update the **Pinned commit** and **Snapshot date** lines above.
