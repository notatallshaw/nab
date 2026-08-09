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
  tree is byte-identical to upstream at the pin. The patch carries:
  - `ranges.py`: `VersionRange.from_bounds`, `snap_bounds`,
    `release_intervals`, and `relation` with its `RangeRelation` result type
    and the member aliases the return paths bind;
    an `assume_sorted` keyword on `filter` and the
    `VersionRange._filter_sorted` it dispatches to; `is_subset` and
    `is_disjoint` answered by a direct walk over the interval lists instead of
    an intermediate range; the module-level helpers they need (`_relate_bounds`,
    `_subset_bounds`, `_disjoint_bounds`, `_bisect_predicate`,
    `_partition_indexes`, `_make_project`, `_check_order`, `_lattice_release`,
    `_release_boundary_point`); the `RangeRelation` and `SortedOrder` `__all__`
    entries; and the class-docstring lines naming them.
  - `_ranges.py`: an unbounded end canonicalizes its inclusivity, and
    `LowerBound.__gt__`, `LowerBound.__le__`, and `UpperBound.__gt__` are
    written out beside `functools.total_ordering`, which still derives the
    rest.
  - `markersets.py` and `_markersets.py`: the marker-algebra module and the
    private engine behind it, both new files, plus the `Marker.to_set`
    accessor they need on `markers.py`.
  - `_parser.py`: `Value.serialize` from pypa/packaging#1213, which merged
    after the pin. A value containing a double quote is emitted single-quoted,
    so `str(Marker)` stays parseable. Drop this hunk once the pin moves past
    `5b583e3`.

  Upstream PRs are planned for the bound ordering, the direct subset and
  disjoint walks, `filter`'s `assume_sorted` fast path, and
  `from_bounds`/`snap_bounds`/`release_intervals`.
  `relation` is deliberately not proposed yet: most of its win is available
  from the direct walks alone, and what is left depends on the interval shapes.
- `tasks/vendor-packaging.sh` fetches `pypa/packaging` at the pin, replaces this
  package with the pristine `src/packaging/` tree plus the repo-root license
  texts, and reapplies the patch. `--check` rebuilds into a temp location and
  fails if the committed tree has drifted. This file is nab's own and stays out
  of both the rebuild and the comparison.
- CI runs `tasks/vendor-packaging.sh --check`, so the committed tree is proven
  to equal pristine-at-pin plus the patch on every push.

## Refreshing

1. Bump `tasks/vendoring/packaging.pin` to the new upstream commit.
2. Merge the two histories per file, three-way: the committed vendored file is
   "ours", pristine at the old pin is the base, pristine at the new pin is
   "theirs". `git merge-file` over the three is enough, since only the files
   the new pin touches need merging.
3. Regenerate the patch from that merged tree, and from the merged tree only.
   Copy it over pristine-at-the-new-pin in a throwaway git repo laid out at
   this directory's repo-relative path, then `git diff` and keep the result
   verbatim. Never hand-edit the patch and never resolve a conflict inside it;
   it is generated output.
4. Run `tasks/vendor-packaging.sh`, then the test suite.

Two traps this order exists to avoid.

Skipping the merge and diffing the old committed tree straight against the new
pin looks like it works and is wrong: the diff then carries deletions for
everything the new pin added, so the patch silently reverts the upstream change
the bump was for. `--check` still passes, because it only proves the committed
tree equals pristine plus the patch, and a reverting patch satisfies that.
Bumping `6f52c6b` to `58c6cd7` this way produced a patch that deleted
`VersionRange.to_specifier_set` and every helper behind it.

The merge auto-resolving is not evidence that it resolved correctly. Check it
by diffing pristine against the merged tree at both pins and comparing the two
diffs: nab's divergence should come out line for line identical, with only the
surrounding context lines moved.

The patch is the accumulated divergence and no single fork branch carries all
of it, so never rebuild it from a branch.

## License

`packaging` is dual-licensed under the Apache License 2.0 and the 2-Clause BSD
License. The LICENSE files here are the upstream texts; nothing is relicensed.

## Why vendor instead of depending on it

`packaging` now ships a public `VersionRange` class with set algebra
(intersection, union, complement, difference), the `is_subset` / `is_superset`
/ `is_disjoint` predicates, `is_empty`, `filter`, a `SpecifierSet.to_range()`
factory that nab's PubGrub solver depends on, and the `to_specifier_set()`
inverse that renders a range back as a specifier set. The class landed in
`main` via https://github.com/pypa/packaging/pull/1267, the difference operator
plus the relation predicates via https://github.com/pypa/packaging/pull/1298,
and `to_specifier_set` via https://github.com/pypa/packaging/pull/1270. None of
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
