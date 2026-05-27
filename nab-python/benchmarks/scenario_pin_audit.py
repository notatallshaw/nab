"""Flag scenarios whose direct requirements cannot be met at their cutoff date.

For each scenario with a ``datetime`` cutoff, every direct (root) requirement is
checked against the warm simple-API cache: if no version satisfying the
requirement was uploaded on or before the cutoff, the scenario is structurally
root-unsat for a data reason rather than a resolver reason. Two cases:

- ``exists-later``: a satisfying version exists but every one was uploaded after
  the cutoff. This is the dating-typo signature; re-dating turns the failure into
  a real resolve.
- ``absent``: no version satisfies the requirement at all. This is a genuine pin
  (deliberate error tests, withdrawn releases).

Offline and deterministic: no resolver run, no network. Pass ``--results DIR`` to
annotate each finding with the recorded pass/fail from a sweep.

Usage:
    python nab-python/benchmarks/scenario_pin_audit.py [--results DIR] [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import scenarios as _scenarios
import tomllib

from nab_python._vcs_admission import UnsupportedVcsError
from nab_python._vendor.packaging.requirements import InvalidRequirement
from nab_python._vendor.packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    parse_sdist_filename,
    parse_wheel_filename,
)
from nab_python._vendor.packaging.version import InvalidVersion, Version
from nab_python.provider import split_extra

if TYPE_CHECKING:
    from nab_python._vendor.packaging.ranges import VersionRange

SIMPLE_CACHE = _scenarios.CACHE_DIR / "simple-v0" / "pypi"


def file_version(filename: str) -> Version | None:
    """Parse a version from a wheel or sdist filename, or None if unparseable."""
    try:
        if filename.endswith(".whl"):
            return parse_wheel_filename(filename)[1]
        return parse_sdist_filename(filename)[1]
    except (InvalidWheelFilename, InvalidSdistFilename, InvalidVersion):
        return None


def earliest_uploads(canonical: str) -> dict[Version, str] | None:
    """Map each cached version to its earliest file upload time.

    Returns None when the package is not in the cache.
    """
    path = SIMPLE_CACHE / f"{canonical}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    out: dict[Version, str] = {}
    for entry in data.get("files", []):
        version = file_version(entry["filename"])
        upload = entry.get("upload-time")
        if version is None or upload is None:
            continue
        if version not in out or upload < out[version]:
            out[version] = upload
    return out


def scenario_requirements(name: str, scenario: dict) -> dict[str, VersionRange]:
    """Build the root requirement ranges a scenario presents to the resolver.

    Mirrors scenarios.process_scenario: folds project extras in and gates root
    markers against the scenario's Python and marker environment.
    """
    overlay = _scenarios.parse_marker_environment(name, scenario)
    env = _scenarios._scenario_marker_env(scenario["python_version"], overlay)
    strings = list(scenario["requirements"])
    if scenario.get("project_name"):
        strings += _scenarios.expand_project_extras(
            scenario["project_name"],
            scenario.get("project_extras", []),
            scenario.get("optional_dependencies", {}),
        )
    return _scenarios.parse_requirements(strings, marker_environment=env)


def audit_scenario(name: str, scenario: dict) -> list[tuple[str, str, str | None]]:
    """Return (package, kind, earliest_satisfying_upload) for each bad pin."""
    cutoff = _scenarios.parse_datetime(scenario["datetime"]).isoformat()
    findings: list[tuple[str, str, str | None]] = []
    for package, version_range in scenario_requirements(name, scenario).items():
        if split_extra(package)[1] is not None:
            continue
        uploads = earliest_uploads(package)
        if not uploads:
            continue
        satisfying = [u for v, u in uploads.items() if v in version_range]
        if not satisfying:
            findings.append((package, "absent", None))
        elif min(satisfying) > cutoff:
            findings.append((package, "exists-later", min(satisfying)))
    return findings


def load_outcome(results_dir: Path, stem: str, name: str) -> str:
    """Return the recorded pass/fail for one scenario, or '?' when absent."""
    path = results_dir / stem / f"{name}.json"
    if not path.exists():
        return "?"
    return "ok" if json.loads(path.read_text())["result"]["success"] else "FAIL"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results",
        type=Path,
        default=None,
        help="A results/<label> directory to annotate findings with pass/fail",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any exists-later (dating-typo) finding is present",
    )
    args = parser.parse_args()

    later: list[str] = []
    absent: list[str] = []
    for toml_file in sorted(_scenarios.SCENARIOS_DIR.glob("*.toml")):
        if toml_file.stem.startswith("universal"):
            continue
        scenarios = tomllib.loads(toml_file.read_text())
        for name, scenario in scenarios.items():
            if "unsupported_reason" in scenario or "datetime" not in scenario:
                continue
            try:
                findings = audit_scenario(name, scenario)
            except (
                InvalidRequirement,
                NotImplementedError,
                UnsupportedVcsError,
                ValueError,
            ):
                continue
            if not findings:
                continue
            outcome = (
                f" [{load_outcome(args.results, toml_file.stem, name)}]"
                if args.results
                else ""
            )
            for package, kind, upload in findings:
                tail = f" earliest {upload}" if upload else ""
                line = (
                    f"{toml_file.stem}:{name}{outcome} {package} {kind}"
                    f" (cutoff {scenario['datetime']}){tail}"
                )
                (later if kind == "exists-later" else absent).append(line)

    print(f"exists-later (dating-typo candidates): {len(later)}")
    for line in later:
        print(f"  {line}")
    print(f"\nabsent (genuine / deliberate pins): {len(absent)}")
    for line in absent:
        print(f"  {line}")

    if args.check and later:
        sys.exit(1)


if __name__ == "__main__":
    main()
