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

# NAB_* is nab's own settings namespace and it rejects any variable in it that
# is not a setting, so the override for which nab to run lives outside it.
NAB=(.venv/bin/python -m nab)
if [[ -n "${REFRESH_NAB_BIN:-}" ]]; then
    read -ra NAB <<<"${REFRESH_NAB_BIN}"
fi

EXTRA_ARGS=("$@")

# Only one interpreter ever builds the docs: Read the Docs and the CI docs job
# both pin 3.13. Locking that group across the project's universal matrix would
# hold the toolchain to whatever still supports the 3.10 floor, so it gets a
# single resolution for the version that actually builds it.  The platform axis
# comes from whoever runs this, which is Linux, as both builders are.
DOCS_PYTHON="3.13"

for group in tests types pre-commit crosshair release docs; do
    echo "==> Locking group: ${group}"
    group_args=()
    if [[ "${group}" == "docs" ]]; then
        group_args=(--project-mode specific --python "${DOCS_PYTHON}")
    fi
    "${NAB[@]}" lock \
        --groups "${group}" \
        --no-emit-workspace \
        --output ".github/requirements/pylock.${group}.toml" \
        "${group_args[@]}" \
        "${EXTRA_ARGS[@]}"
done

echo "==> Done"
ls -1 .github/requirements/pylock.*.toml
