# Vendored `packaging` snapshot

This directory contains `pypa/packaging` at the commit in `tasks/vendoring/packaging.pin`, plus the local changes in `tasks/vendoring/packaging.patch`. The upstream license texts are included without modification.

## Remaining changes

The patch changes two modules:

- `markers.py` adds `prepare_environment` and `Marker.evaluate_prepared` for evaluating multiple markers against one prepared environment. Ordinary `Marker.evaluate` retains its upstream implementation.
- `ranges.py` adds `VersionRange.from_bounds`, `snap_bounds`, and `relation` with its `RangeRelation` result type. It also retains the direct interval walk for `is_disjoint` and the helpers these operations need.

Quoted-string parsing, marker formatting and serialization caching, unbounded-bound normalization, cached-key bound comparisons, interval subtraction and the subset check use the upstream implementations.

## Rebuilding

`tasks/vendor-packaging.sh` fetches the pinned commit, copies `src/packaging/` and the root license texts, and applies the patch. Without arguments it replaces the vendored tree. `--check` rebuilds in a temporary directory and compares the result with the committed tree. This provenance file is maintained separately and excluded from that comparison.

CI runs the reconstruction check on pull requests and main.

## Refreshing

1. Update `tasks/vendoring/packaging.pin` to the desired upstream commit.
2. Compare pristine source at the old pin, the committed vendored tree, and pristine source at the new pin. Merge changes per source file and adopt upstream implementations for local changes that have landed there.
3. Regenerate the patch by diffing pristine source at the new pin against the merged tree, using this directory's repository-relative path. Never edit the patch by hand or resolve conflicts inside it.
4. Rebuild with `tasks/vendor-packaging.sh`, check the remaining divergence, and run the tests.

Diffing the old vendored tree directly against a new pin can generate a patch that reverts upstream additions. The reconstruction check cannot detect that mistake: it establishes only that the committed tree equals the pin plus the patch. Review the source changes and the remaining patch separately.

## Removing the vendored copy

Nab still needs the APIs listed above. Vendoring can end when those APIs are available in a supported `packaging` release and no local patch remains:

- Add the required `packaging` version to `nab-provider/pyproject.toml`.
- Replace `from nab_provider._vendor.packaging` with `from packaging` and `import nab_provider._vendor.packaging` with `import packaging` throughout the workspace.
- Remove the vendored backend from `nab_markersets._packaging.BACKENDS` and retire its `nab-vendored-packaging` extra.
- Remove this tree and the vendoring files.

The marker algebra lives in `nab-markersets`, which supports both this vendored backend and released `packaging`.
