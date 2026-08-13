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

# The consistency checks below parse pyproject files rather than run nab, so
# they need an interpreter of their own. tomli comes from nab-project, which the
# project venv always has, and unlike tomllib it is there on the 3.10 floor.
PYTHON="${REFRESH_PYTHON:-.venv/bin/python}"

EXTRA_ARGS=("$@")

# Only one interpreter ever builds the docs: Read the Docs and the CI docs job
# both pin 3.13. Locking that group across the project's universal matrix would
# hold the toolchain to whatever still supports the 3.10 floor, so it gets a
# single resolution for the version that actually builds it.  The platform axis
# comes from whoever runs this, which is Linux, as both builders are.
DOCS_PYTHON="3.13"

# Each lock covers a single group, so that group is also its default: pip and
# uv seed the PEP 751 dependency_groups marker from default-groups and cannot
# select a non-default group at install time, so without this the group's
# packages carry a marker no install-time flag ever satisfies and are skipped.
# One build lock serves every package here, which holds only while they agree.
# Checked before any lock is rewritten, so a mismatch leaves the tree untouched.
echo "==> Checking every package declares the same build system"
"${PYTHON}" - <<'PY'
import sys
import tomli
from pathlib import Path

KEYS = ("requires", "build-backend", "backend-path")
declared = {}
for path in [Path("pyproject.toml"), *sorted(Path().glob("nab-*/pyproject.toml"))]:
    table = tomli.loads(path.read_text())["build-system"]
    declared[path] = tuple(str(table.get(key)) for key in KEYS)

if len(set(declared.values())) > 1:
    for path, values in declared.items():
        print(f"  {path}: {dict(zip(KEYS, values))}", file=sys.stderr)
    sys.exit(
        "packages disagree on [build-system], so one build lock cannot serve"
        " them all; align them or lock each package separately"
    )
PY

for group in tests types pre-commit crosshair release docs nox dists; do
    echo "==> Locking group: ${group}"
    group_args=()
    if [[ "${group}" == "docs" ]]; then
        group_args=(--project-mode specific --python "${DOCS_PYTHON}")
    fi
    "${NAB[@]}" lock \
        --groups "${group}" \
        --project-default-group "${group}" \
        --no-emit-workspace \
        --output ".github/requirements/pylock.${group}.toml" \
        "${group_args[@]}" \
        "${EXTRA_ARGS[@]}"
done

echo "==> Locking [build-system].requires"
"${NAB[@]}" lock \
    --build-requirements \
    --no-emit-workspace \
    --output ".github/requirements/pylock.build.toml" \
    "${EXTRA_ARGS[@]}"

# The build lock is installed alongside the dists and release locks, so a
# package they share has to resolve the same in both or the second install
# silently moves the first.
echo "==> Checking the locks that share an environment agree"
"${PYTHON}" - <<'PY'
import sys
import tomli
from pathlib import Path


def pins(name):
    lock = tomli.loads(Path(f".github/requirements/pylock.{name}.toml").read_text())
    versions = {}
    for package in lock["packages"]:
        versions.setdefault(package["name"], set()).add(package.get("version"))
    return versions


build = pins("build")
disagreed = [
    f"  {name}: {package} is {sorted(build[package])} in the build lock"
    f" and {sorted(other[package])} there"
    for name in ("dists", "release")
    for other in [pins(name)]
    for package in sorted(build.keys() & other.keys())
    if build[package] != other[package]
]
if disagreed:
    print("\n".join(disagreed), file=sys.stderr)
    sys.exit("locks installed into one environment disagree on a shared package")
PY

echo "==> Done"
ls -1 .github/requirements/pylock.*.toml
