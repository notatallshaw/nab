# Vendored `packaging` snapshot

This directory holds an unmodified copy of the `packaging` library,
vendored so nab can use the public `VersionRange` API before a
`packaging` release containing it is published to PyPI.

## Source

- Repository: https://github.com/notatallshaw/packaging
- Source branch: `experiment-prerelease-range`
- Pinned commit: `a6a6d038d55b3a265670efb84dc06e428fd302c9`
- Snapshot date: 2026-06-29

This branch is the head of `pypa/packaging` PR
https://github.com/pypa/packaging/pull/1304. It descends directly from
`pypa/packaging:main` (the previous snapshot commit
`e80f70f8b164ee4b0ef7634aafe8c6cc4ce00ca3` is its merge base) and adds
the pre-release-region model described below, plus other commits already
merged to `main`.

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

`packaging` ships a public `VersionRange` class with set algebra
(intersection, union, complement, difference), `is_empty`, `filter`, and
a `SpecifierSet.to_range()` factory that nab's PubGrub solver depends on.
The class landed in `main` via
https://github.com/pypa/packaging/pull/1267 but has not yet appeared in
a PyPI release, so there is no version we can pin in `pyproject.toml`.
Vendoring is a temporary measure.

## Why this branch and not `pypa/packaging:main`

The solver derives a package's effective range as `positive & ~negative`,
where `negative` is an exclusion (an excluded version range, e.g. left
behind by a backtracked branch). On `main`, a `VersionRange` modeled its
pre-release opt-in as a single policy flag, so the complement of an
exclusion that named a pre-release carried that opt-in onto the result.
The intersection then admitted pre-releases no active requirement asked
for, and an unrelated pre-release could beat a final.

PR #1304 scopes the pre-release opt-in to a region of versions rather
than a whole-range flag, and tracks that region through set algebra. A
complement (an exclusion) carries no opt-in, so `a & ~b` sheds `b`'s
opt-in and admits no pre-release `b` named. This makes
`positive & ~negative` correct for the pre-release policy without any
change to the solver. The pinned commit includes the follow-up fix that
drops the opt-in on complement (`fix(ranges): drop pre-release opt-in on
complement`); the published region model alone left a residual leak when
an excluded pre-release specifier also carried an upper bound.

## Removal plan

Delete this entire directory and reinstate `packaging` as a normal
dependency once a `packaging` release containing the merged
`VersionRange` class (including the PR #1304 pre-release-region model)
has been published to PyPI.

Once that holds:

- Add `packaging>=<release>` back to `nab-python/pyproject.toml`
  `[project].dependencies`.
- Search-replace `from nab_python._vendor.packaging` ->
  `from packaging` and `import nab_python._vendor.packaging` ->
  `import packaging` across the workspace.
- Remove this `_vendor/` tree.

## Updating the snapshot

While PR #1304 is unmerged, refresh against its branch:

```
cd packaging  # the notatallshaw/packaging fork checkout
git fetch origin experiment-prerelease-range
REF=origin/experiment-prerelease-range
NEW_SHA=$(git rev-parse $REF)
DEST=/path/to/nab/nab-python/src/nab_python/_vendor/packaging
for f in $(git ls-tree -r --name-only $REF -- src/packaging); do
    rel=${f#src/packaging/}
    mkdir -p "$(dirname "$DEST/$rel")"
    git show "$REF:$f" > "$DEST/$rel"
done
for f in LICENSE LICENSE.APACHE LICENSE.BSD; do
    git show "$REF:$f" > "$DEST/$f"
done
```

Once PR #1304 merges, switch `REF` back to `upstream/main`. Then update
the **Source**, **Pinned commit**, and **Snapshot date** lines above.
