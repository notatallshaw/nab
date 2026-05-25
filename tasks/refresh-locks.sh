#!/usr/bin/env bash
# Regenerate the CI lockfiles under .github/requirements/ by running
# nab against its own dependency-groups.
#
# The P7D cooldown ([tool.nab].uploaded-prior-to) is anchored to the
# existing pylock's created-at by default. Pass --upgrade to reset
# the anchor to "now".

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

NAB=(.venv/bin/python -m nab)
if [[ -n "${NAB_BIN:-}" ]]; then
    read -ra NAB <<<"${NAB_BIN}"
fi

EXTRA_ARGS=("$@")

for group in tests types pre-commit crosshair release; do
    echo "==> Locking group: ${group}"
    "${NAB[@]}" lock \
        --groups "${group}" \
        --no-emit-workspace \
        --output ".github/requirements/pylock.${group}.toml" \
        "${EXTRA_ARGS[@]}"
done

echo "==> Done"
ls -1 .github/requirements/pylock.*.toml
