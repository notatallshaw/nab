#!/usr/bin/env bash
# Rebuild the vendored packaging tree as pristine pypa/packaging at the pinned
# commit plus one checked-in patch.
#
# No arguments regenerates the tree in place. --check rebuilds into a temp
# location and fails if the committed tree has drifted from pristine plus the
# patch. PROVENANCE.md is nab's own file and never part of the comparison.

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
VENDOR="$ROOT/nab-python/src/nab_python/_vendor/packaging"
PIN_FILE="$ROOT/tasks/vendoring/packaging.pin"
PATCH_FILE="$ROOT/tasks/vendoring/packaging.patch"
REMOTE="https://github.com/pypa/packaging"
VENDOR_REL="nab-python/src/nab_python/_vendor/packaging"

MODE="regenerate"
if [[ $# -gt 0 ]]; then
    case "$1" in
        --check) MODE="check" ;;
        *) echo "usage: $(basename "$0") [--check]" >&2; exit 2 ;;
    esac
fi

PIN="$(tr -d '[:space:]' < "$PIN_FILE")"
if [[ -z "$PIN" ]]; then
    echo "error: no pin in $PIN_FILE" >&2
    exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Fetch the pinned commit shallowly into a throwaway repo.
SRC="$WORK/packaging"
git init -q "$SRC"
git -C "$SRC" fetch -q --depth 1 "$REMOTE" "$PIN"
git -C "$SRC" checkout -q FETCH_HEAD

# Stage the pristine package plus the license texts from the repo root, laid out
# at the same repo-relative path the patch is rooted at so it applies verbatim.
STAGE="$WORK/tree/$VENDOR_REL"
mkdir -p "$STAGE"
cp -a "$SRC/src/packaging/." "$STAGE/"
for lic in LICENSE LICENSE.APACHE LICENSE.BSD; do
    cp -a "$SRC/$lic" "$STAGE/$lic"
done

if [[ -f "$PATCH_FILE" ]]; then
    git -C "$WORK/tree" apply "$PATCH_FILE"
fi

if [[ "$MODE" == "check" ]]; then
    if diff -ru -x __pycache__ -x PROVENANCE.md "$VENDOR" "$STAGE"; then
        echo "vendored packaging matches pin $PIN plus the patch"
        exit 0
    fi
    echo "error: vendored tree drifted from pin $PIN plus the patch" >&2
    echo "run tasks/vendor-packaging.sh to regenerate it" >&2
    exit 1
fi

# Regenerate in place, keeping nab's own PROVENANCE.md.
cp -a "$VENDOR/PROVENANCE.md" "$STAGE/PROVENANCE.md"
rm -rf "$VENDOR"
mkdir -p "$(dirname "$VENDOR")"
cp -a "$STAGE" "$VENDOR"
echo "vendored packaging regenerated at pin $PIN"
