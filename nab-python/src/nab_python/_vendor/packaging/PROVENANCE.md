# Vendored `packaging` snapshot

This directory holds an unmodified copy of the `packaging` library taken
from an in-flight pull request, vendored so nab can use the public
`VersionRange` API before the PR is merged and released.

## Source

- Upstream repository: https://github.com/pypa/packaging
- Pull request: https://github.com/pypa/packaging/pull/1182
- Source branch: `notatallshaw/packaging:public-pep440-version-range`
- Pinned commit: `15a0c21f07983dddde5d61188e537df13bf1d3e5`
- Snapshot date: 2026-05-31

The snapshot is the full `src/packaging/` tree at that commit, plus
`LICENSE`, `LICENSE.APACHE`, and `LICENSE.BSD` from the repository
root. Relative imports inside `packaging` (`from .version import
Version`, etc.) keep working when the package is loaded as
`nab_python._vendor.packaging`.

## Local patches

Changes on top of the pinned commit. Carry these forward (or confirm
they landed on the source branch) when updating the snapshot.

- `markers.py`: `_normalize_extras` recurses into nested
  `MarkerList`s so extra values inside parenthesized marker groups
  are PEP 685-normalized. Without this, `(extra == "My_Extra")`
  evaluates differently from the unparenthesized form and deps are
  silently dropped from the extra. Also present in upstream
  `packaging` 26.2; not yet fixed there.

## License

`packaging` is dual-licensed under the Apache License 2.0 and the
2-Clause BSD License. The LICENSE files in this directory are the
upstream texts; nothing here is relicensed.

## Why vendor instead of depending on it

`packaging` PR 1182 promotes the internal range helpers to a public
`VersionRange` class with set algebra (intersection, union,
complement) and `to_range()` / `from_specifier_set()` factories that
nab's PubGrub solver depends on. The PR is open and has not yet been
released on PyPI, so there is no version we can pin in
`pyproject.toml`. Vendoring is a temporary measure.

## Removal plan

Delete this entire directory and reinstate `packaging` as a normal
dependency once **both** are true:

1. PR 1182 has merged into `pypa/packaging:main`.
2. A `packaging` release containing the merged commit has been
   published to PyPI.

Once those hold:

- Add `packaging>=<release>` back to `nab-python/pyproject.toml`
  `[project].dependencies`.
- Search-replace `from nab_python._vendor.packaging` ->
  `from packaging` and `import nab_python._vendor.packaging` ->
  `import packaging` across the workspace.
- Remove this `_vendor/` tree.

## Updating the snapshot

If PR 1182 is updated and nab needs the newer code:

```
cd packaging  # the upstream fork checkout
git fetch origin public-pep440-version-range
NEW_SHA=$(git rev-parse origin/public-pep440-version-range)
DEST=/path/to/nab/nab-python/src/nab_python/_vendor/packaging
for f in $(git ls-tree -r --name-only origin/public-pep440-version-range -- src/packaging); do
    rel=${f#src/packaging/}
    mkdir -p "$(dirname "$DEST/$rel")"
    git show "origin/public-pep440-version-range:$f" > "$DEST/$rel"
done
for f in LICENSE LICENSE.APACHE LICENSE.BSD; do
    git show "origin/public-pep440-version-range:$f" > "$DEST/$f"
done
```

Then update the **Pinned commit** and **Snapshot date** lines above.
