# Vendored `packaging` snapshot

This directory contains unmodified `pypa/packaging` at the commit in `tasks/vendoring/packaging.pin`, including the upstream license texts. No local patches remain.

## Rebuilding

`tasks/vendor-packaging.sh` fetches the pinned commit and copies `src/packaging/` and the root license texts. Without arguments it replaces the vendored tree. `--check` rebuilds in a temporary directory and compares the result with the committed tree. This provenance file is maintained separately and excluded from that comparison.

CI runs the reconstruction check on pull requests and main.

## Refreshing

Update `tasks/vendoring/packaging.pin`, rebuild with `tasks/vendor-packaging.sh`, and run the tests.

## Removing the vendored copy

Nab requires prepared marker environments and the range construction, relation and snapping APIs now present in the pinned snapshot. Vendoring can end when those APIs are available in a supported `packaging` release:

- Add the required `packaging` version to `nab-provider/pyproject.toml`.
- Replace imports of `nab_provider._vendor.packaging` with imports of `packaging` throughout the workspace.
- Remove the vendored backend from `nab_markersets._packaging.BACKENDS` and retire its `nab-vendored-packaging` extra.
- Remove this tree and the vendoring files.

The marker algebra lives in `nab-markersets`, which supports both this vendored backend and released `packaging`.
