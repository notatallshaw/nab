"""Run the offline deterministic resolver smoke suite.

Usage:
    python nab-project/benchmarks/deterministic_smoke.py [--runs N]
    python nab-project/benchmarks/deterministic_smoke.py --scenario ID --json out.json
    python nab-project/benchmarks/deterministic_smoke.py \
        --fixture-dir /tmp/nab-smoke-index --materialize-only
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import json
import socket
import stat
import statistics
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn
from urllib.parse import urlparse
from urllib.request import url2pathname

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from nab_provider._vendor.packaging.pylock import Package, Pylock

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from nab_index.multi_index import IndexConfig
from nab_index.urllib3_async_transport import Urllib3AsyncTransport
from nab_project.config import NabProjectConfig, enforce_build_policy_for_targets
from nab_project.fetch import FetchCoordinator
from nab_project.lockfile import LOCK_VERSION, build_pylock
from nab_project.resolve import (
    ResolveResult,
    build_lock_input,
    resolve_with_coordinator,
)
from nab_provider._vendor.packaging.markers import Marker
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider._vendor.packaging.version import Version
from nab_provider.provider import ResolutionStrategy
from nab_provider.tags import PlatformSpec
from nab_provider.target import Matrix, ResolveTarget, environment_declaration
from nab_resolver.errors import ResolutionError

BENCHMARKS_DIR = Path(__file__).parent
SMOKE_DIR = BENCHMARKS_DIR / "smoke"
FIXTURE_PATH = SMOKE_DIR / "fixture.toml"
SCENARIOS_PATH = SMOKE_DIR / "scenarios.toml"

# Both go into every report, so a timing number is never read without the
# boundary it was taken at.
TIMING_BOUNDARY = (
    "Each raw inner interval wraps only resolve_with_coordinator with pre-parsed "
    "root requirements; fixture generation, coordinator lifecycle, warmups, "
    "semantic validation, and lock emission are excluded. An aggregate sample "
    "is the sum of batch_size raw intervals."
)
CACHE_MODE = (
    "Each resolve uses a fresh FetchCoordinator against one shared, prebuilt "
    "offline file Simple index, so resolver and HTTP caches are logically cold. "
    "Untimed batches warm process state and OS filesystem caches before measured "
    "batches."
)
# The zip epoch, so a wheel's bytes depend only on its metadata and the fixture
# digest stays stable across machines and runs.
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

# Every table in both manifests is closed: a key outside these sets is an error
# rather than a silently ignored typo.
_FIXTURE_DOCUMENT_KEYS = frozenset({"fixture", "package", "family"})
_FIXTURE_KEYS = frozenset({"schema", "sha256"})
_PACKAGE_KEYS = frozenset(
    {"name", "version", "dependencies", "requires-python", "provides-extra"}
)
_FAMILY_KEYS = frozenset({"kind", "prefix", "size"})

_SCENARIO_DOCUMENT_KEYS = frozenset({"suite", "scenario"})
_SUITE_KEYS = frozenset({"schema"})
_SCENARIO_KEYS = frozenset(
    {
        "id",
        "provenance",
        "purpose",
        "lane",
        "outcome",
        "warmups",
        "batch-size",
        "mode",
        "requirements",
        "constraints",
        "python",
        "platforms",
        "resolution",
        "align-across-targets",
        "expected",
    }
)
_EXPECTED_KEYS = frozenset({"target", "pins"})
_REQUIRED_SCENARIO_KEYS = (
    "id",
    "provenance",
    "purpose",
    "lane",
    "outcome",
    "mode",
    "requirements",
    "python",
    "platforms",
)
_STRING_SCENARIO_KEYS = (
    "id",
    "provenance",
    "purpose",
    "lane",
    "outcome",
    "mode",
    "python",
)

__all__ = [
    "FIXTURE_PATH",
    "SCENARIOS_PATH",
    "Distribution",
    "PreparedScenario",
    "Scenario",
    "SmokeContractError",
    "file_sha256",
    "fixture_access_identity",
    "fixture_digest",
    "load_fixture",
    "load_scenarios",
    "materialize_fixture",
    "prepare_scenario",
    "validate_materialized_fixture",
    "validate_nab_lock",
]


class SmokeContractError(Exception):
    """The frozen fixture, scenario declaration, or resolver output is invalid."""


def _fail(message: str) -> NoReturn:
    raise SmokeContractError(message)


def _asyncio_wakeup_roundtrip() -> None:
    """Exercise the socket transport used by asyncio cross-thread wakeups."""
    reader, writer = socket.socketpair()
    with reader, writer:
        reader.settimeout(1.0)
        writer.settimeout(1.0)
        if writer.send(b"\0") != 1 or reader.recv(1) != b"\0":
            msg = "socketpair round-trip was incomplete"
            raise OSError(msg)


def _validate_asyncio_wakeup_transport(
    *, probe: Callable[[], None] = _asyncio_wakeup_roundtrip
) -> None:
    """Fail on a sandbox that blocks socketpair, before a resolve hangs on it.

    The fetch coordinator drives asyncio from another thread, and asyncio wakes
    its loop through a socket pair. A policy that denies one turns every resolve
    into a timeout with nothing to read from it.
    """
    try:
        probe()
    except OSError as exc:
        detail = type(exc).__name__
        if exc.errno is not None:
            detail += f" (errno {exc.errno})"
        _fail(
            "benchmark environment cannot use the socketpair transport"
            f" required by asyncio cross-thread wakeups: {detail}"
        )


def _reject_unknown(
    raw: Mapping[str, object], allowed: frozenset[str], label: str
) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        _fail(f"{label} has unknown keys: {unknown}")


@dataclass(frozen=True, slots=True)
class Distribution:
    """One wheel in the frozen fixture universe."""

    name: str
    version: str
    dependencies: tuple[str, ...] = ()
    requires_python: str | None = None
    provides_extra: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExpectedTarget:
    """Expected package selection for one declared target."""

    target: str
    pins: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Scenario:
    """One scenario and its optional timing work factor."""

    id: str
    provenance: str
    purpose: str
    lane: str
    outcome: str
    warmups: int
    batch_size: int
    mode: str
    requirements: tuple[str, ...]
    constraints: tuple[str, ...]
    python: str
    platforms: tuple[str, ...]
    resolution: ResolutionStrategy | None
    align_across_targets: bool | None
    expected: tuple[ExpectedTarget, ...]


@dataclass(frozen=True, slots=True)
class PreparedScenario:
    """Parsed resolver inputs and policy created outside every timed interval."""

    scenario: Scenario
    targets: tuple[ResolveTarget, ...]
    index: IndexConfig
    config: NabProjectConfig
    requirements: tuple[Requirement, ...]
    expected: Mapping[str, Mapping[str, str]]
    align_across_targets: bool


@dataclass(frozen=True, slots=True)
class _TargetSearch:
    """Resolver search counters for one target, compared across repeated runs."""

    target: str
    decisions: int
    rounds: int
    conflicts: int
    backjumps: int


@dataclass(frozen=True, slots=True)
class _ScenarioObservation:
    """Everything one resolve produced: its timing and its semantic projection."""

    elapsed_ns: int
    pins: dict[str, dict[str, str]]
    lock_projection: dict[str, dict[str, str]] | None
    failures: dict[str, dict[str, object]]
    search: tuple[_TargetSearch, ...]
    fetch: tuple[tuple[str, int, int], ...]


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _fail(f"cannot read {path}: {exc}")


def _as_strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail(f"{label} must be an array of strings")
    return tuple(value)


def _validate_name(value: str, label: str) -> None:
    try:
        canonicalize_name(value, validate=True)
    except ValueError as exc:
        _fail(f"{label} is not a valid distribution name: {exc}")


def _validate_version(value: str, label: str) -> None:
    try:
        Version(value)
    except ValueError as exc:
        _fail(f"{label} is not a valid version: {exc}")


def _distribution(raw: Mapping[str, object], label: str) -> Distribution:
    """Parse one `[[package]]` table."""
    _reject_unknown(raw, _PACKAGE_KEYS, label)

    try:
        name = raw["name"]
        version = raw["version"]
    except KeyError as exc:
        _fail(f"{label} is missing {exc.args[0]!r}")
    if not isinstance(name, str) or not isinstance(version, str):
        _fail(f"{label} name and version must be strings")
    _validate_name(name, f"{label}.name")
    _validate_version(version, f"{label}.version")

    dependencies = _as_strings(raw.get("dependencies", []), f"{label}.dependencies")
    extras = _as_strings(raw.get("provides-extra", []), f"{label}.provides-extra")
    for extra in extras:
        _validate_name(extra, f"{label}.provides-extra")

    requires_python = raw.get("requires-python")
    if requires_python is not None and not isinstance(requires_python, str):
        _fail(f"{label}.requires-python must be a string")

    return Distribution(name, version, dependencies, requires_python, extras)


def _pip_backtracking_family(
    prefix: str, size: int, *, unsatisfiable: bool
) -> list[Distribution]:
    """Build pip's deep-backtracking graph.

    For each version N of `<prefix>-a`, `a` wants `b==N` and `c==N-1` while `b==N`
    wants `c==N`, so every candidate above the first pins `c` to two versions at
    once. The solver has to reject all of them before reaching `a==1.0.0`. The
    unsatisfiable variant points that last candidate at a version of `c` the
    fixture does not publish, so no candidate survives.

    Every conflict here names the decision one level up, so the solver's backjumps
    all travel a single level. `_deep_backjump_family` is what exercises the
    non-chronological case.
    """
    out: list[Distribution] = []
    for number in range(1, size + 1):
        version = f"{number}.0.0"
        dependencies = [f"{prefix}-b=={version}"]
        if number > 1:
            dependencies.append(f"{prefix}-c=={number - 1}.0.0")
        elif unsatisfiable:
            dependencies.append(f"{prefix}-c==0.0.0")
        out.extend(
            (
                Distribution(f"{prefix}-a", version, tuple(dependencies)),
                Distribution(f"{prefix}-b", version, (f"{prefix}-c=={version}",)),
                Distribution(f"{prefix}-c", version),
            )
        )
    return out


def _deep_backjump_family(prefix: str, size: int) -> list[Distribution]:
    """Build a graph whose conflicts sit many decision levels below the culprit.

    `pivot` is decided early, then a chain of `link` packages is walked one level
    at a time, each discovering the next. Only the last link reveals `zgate`, which
    demands the pivot's oldest version. The conflict therefore names a decision
    made `size` levels earlier, and a solver that backjumps chronologically has to
    re-derive every level in between.

    The version counts and the names both carry weight. The solver decides the
    package with the fewest candidates first, so `zgate` (1) precedes `pivot` (3),
    then `link` (4), then `alt` (5); the names sort in the opposite order, so a
    decision heuristic that stops consulting priority and falls back to its name
    tiebreak walks the graph differently and moves the recorded counters.
    """
    out = [Distribution(f"{prefix}-pivot", f"{number}.0.0") for number in (1, 2, 3)]
    for index in range(1, size + 1):
        successor = f"{prefix}-zgate" if index == size else f"{prefix}-link-{index + 1}"
        dependencies = (successor, f"{prefix}-alt-{index}")
        out.extend(
            Distribution(f"{prefix}-link-{index}", f"{number}.0.0", dependencies)
            for number in (1, 2, 3, 4)
        )
        out.extend(
            Distribution(f"{prefix}-alt-{index}", f"{number}.0.0")
            for number in (1, 2, 3, 4, 5)
        )
    out.append(Distribution(f"{prefix}-zgate", "1.0.0", (f"{prefix}-pivot==1.0.0",)))
    return out


def _expand_family(raw: Mapping[str, object], label: str) -> list[Distribution]:
    """Expand a `[[family]]` table into the graph its kind describes."""
    _reject_unknown(raw, _FAMILY_KEYS, label)

    kind = raw.get("kind")
    if kind not in {
        "pip-deep-backtracking",
        "pip-deep-backtracking-unsatisfiable",
        "deep-backjump",
    }:
        _fail(f"{label}.kind is not supported")
    prefix = raw.get("prefix")
    size = raw.get("size")
    if (
        not isinstance(prefix, str)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 2
    ):
        _fail(f"{label} needs a string prefix and size >= 2")
    _validate_name(prefix, f"{label}.prefix")

    if kind == "deep-backjump":
        return _deep_backjump_family(prefix, size)
    return _pip_backtracking_family(
        prefix,
        size,
        unsatisfiable=kind == "pip-deep-backtracking-unsatisfiable",
    )


def load_fixture(path: Path = FIXTURE_PATH) -> tuple[list[Distribution], str]:
    """Load and expand the fixture manifest, returning it with its pinned digest."""
    document = _read_toml(path)
    _reject_unknown(document, _FIXTURE_DOCUMENT_KEYS, "fixture document")

    fixture = document.get("fixture")
    if not isinstance(fixture, dict):
        _fail("fixture must be a table")
    _reject_unknown(fixture, _FIXTURE_KEYS, "fixture")
    schema = fixture.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema != 1:
        _fail("fixture.schema must be 1")

    expected_digest = fixture.get("sha256")
    if (
        not isinstance(expected_digest, str)
        or len(expected_digest) != 64
        or any(character not in "0123456789abcdef" for character in expected_digest)
    ):
        _fail("fixture.sha256 must be a lowercase 64-character hex digest")

    packages = document.get("package", [])
    families = document.get("family", [])
    if not isinstance(packages, list):
        _fail("package must be an array of tables")
    if not isinstance(families, list):
        _fail("family must be an array of tables")

    distributions: list[Distribution] = []
    for index, raw in enumerate(packages):
        if not isinstance(raw, dict):
            _fail(f"package[{index}] must be a table")
        distributions.append(_distribution(raw, f"package[{index}]"))
    for index, raw in enumerate(families):
        if not isinstance(raw, dict):
            _fail(f"family[{index}] must be a table")
        distributions.extend(_expand_family(raw, f"family[{index}]"))
    if not distributions:
        _fail("fixture must contain at least one package")

    keys = [
        (canonicalize_name(dist.name, validate=True), dist.version)
        for dist in distributions
    ]
    if len(keys) != len(set(keys)):
        _fail("fixture contains duplicate name/version records")

    # The suite runs offline, so a dependency on a name the fixture never
    # publishes would surface as a resolve failure rather than a manifest bug.
    names = {name for name, _version in keys}
    for dist in distributions:
        for dependency in dist.dependencies:
            requirement = Requirement(dependency)
            if canonicalize_name(requirement.name, validate=True) not in names:
                _fail(
                    f"{dist.name} {dist.version} requires absent fixture package"
                    f" {requirement.name}"
                )

    return distributions, expected_digest


def _parse_timing_work(
    raw: Mapping[str, object], label: str, lane: str
) -> tuple[int, int]:
    """Parse the warmup and batch counts, which only the performance lane may set."""
    warmups = raw.get("warmups", 0)
    batch_size = raw.get("batch-size", 1)
    if not isinstance(warmups, int) or isinstance(warmups, bool) or warmups < 0:
        _fail(f"{label}.warmups must be a nonnegative integer")
    if (
        not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or batch_size < 1
    ):
        _fail(f"{label}.batch-size must be a positive integer")
    if lane == "performance" and not {"warmups", "batch-size"} <= raw.keys():
        _fail(f"{label} performance cases require warmups and batch-size")
    if lane == "semantic" and ({"warmups", "batch-size"} & raw.keys()):
        _fail(f"{label} semantic cases cannot set timing work factors")
    return warmups, batch_size


def _parse_expected_targets(
    raw: Mapping[str, object], label: str, outcome: str
) -> tuple[ExpectedTarget, ...]:
    """Parse the per-target pins a scenario must produce."""
    expected_raw = raw.get("expected", [])
    if not isinstance(expected_raw, list) or not expected_raw:
        _fail(f"{label}.expected must not be empty")

    expected: list[ExpectedTarget] = []
    for index, target_raw in enumerate(expected_raw):
        target_label = f"{label}.expected[{index}]"
        if not isinstance(target_raw, dict):
            _fail(f"{target_label} must be a table")
        _reject_unknown(target_raw, _EXPECTED_KEYS, target_label)
        target = target_raw.get("target")
        pins = target_raw.get("pins")
        if not isinstance(target, str) or not isinstance(pins, dict):
            _fail(f"{target_label} needs target and pins")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in pins.items()):
            _fail(f"{target_label}.pins must map strings to strings")
        for name, version in pins.items():
            _validate_name(name, f"{target_label}.pins name")
            _validate_version(version, f"{target_label}.pins[{name!r}]")
        if outcome == "satisfiable" and not pins:
            _fail(f"{target_label}.pins must not be empty for a satisfiable case")
        if outcome == "unsatisfiable" and pins:
            _fail(f"{target_label}.pins must be empty for an unsatisfiable case")
        expected.append(ExpectedTarget(target, dict(pins)))

    target_ids = [item.target for item in expected]
    if len(target_ids) != len(set(target_ids)):
        _fail(f"{label}.expected target ids must be unique")
    return tuple(expected)


def _parse_scenario(raw: Mapping[str, object], index: int) -> Scenario:
    """Parse one `[[scenario]]` table into its declared resolver inputs."""
    label = f"scenario[{index}]"
    _reject_unknown(raw, _SCENARIO_KEYS, label)
    missing = [key for key in _REQUIRED_SCENARIO_KEYS if key not in raw]
    if missing:
        _fail(f"{label} is missing {missing}")
    if not all(isinstance(raw[key], str) for key in _STRING_SCENARIO_KEYS):
        _fail(f"{label} string fields must be strings")

    lane = str(raw["lane"])
    if lane not in {"semantic", "performance"}:
        _fail(f"{label}.lane must be semantic or performance")
    outcome = str(raw["outcome"])
    if outcome not in {"satisfiable", "unsatisfiable"}:
        _fail(f"{label}.outcome must be satisfiable or unsatisfiable")
    warmups, batch_size = _parse_timing_work(raw, label, lane)

    mode = str(raw["mode"])
    if mode not in {"specific", "universal"}:
        _fail(f"{label}.mode must be specific or universal")
    # Absent means "leave the product default alone", which prepare_scenario and
    # _resolve_once each honour by not passing the argument at all.
    alignment = raw.get("align-across-targets")
    if alignment is not None and not isinstance(alignment, bool):
        _fail(f"{label}.align-across-targets must be a boolean")

    resolution = None
    if "resolution" in raw:
        try:
            resolution = ResolutionStrategy(str(raw["resolution"]))
        except ValueError as exc:
            _fail(f"{label}.resolution is invalid: {exc}")

    expected = _parse_expected_targets(raw, label, outcome)
    requirements = _as_strings(raw.get("requirements", []), f"{label}.requirements")
    platforms = _as_strings(raw["platforms"], f"{label}.platforms")
    if not requirements:
        _fail(f"{label}.requirements must not be empty")
    if not platforms:
        _fail(f"{label}.platforms must not be empty")

    return Scenario(
        id=str(raw["id"]),
        provenance=str(raw["provenance"]),
        purpose=str(raw["purpose"]),
        lane=lane,
        outcome=outcome,
        warmups=warmups,
        batch_size=batch_size,
        mode=mode,
        requirements=requirements,
        constraints=_as_strings(raw.get("constraints", []), f"{label}.constraints"),
        python=str(raw["python"]),
        platforms=platforms,
        resolution=resolution,
        align_across_targets=alignment,
        expected=expected,
    )


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[Scenario]:
    """Load and validate the scenario manifest in declaration order."""
    document = _read_toml(path)
    _reject_unknown(document, _SCENARIO_DOCUMENT_KEYS, "scenario document")

    suite = document.get("suite")
    if not isinstance(suite, dict):
        _fail("suite must be a table")
    _reject_unknown(suite, _SUITE_KEYS, "suite")
    schema = suite.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema != 1:
        _fail("suite.schema must be 1")

    raw_scenarios = document.get("scenario", [])
    if not isinstance(raw_scenarios, list):
        _fail("scenario must be an array of tables")
    scenarios: list[Scenario] = []
    for index, raw in enumerate(raw_scenarios):
        if not isinstance(raw, dict):
            _fail(f"scenario[{index}] must be a table")
        scenarios.append(_parse_scenario(raw, index))

    ids = [scenario.id for scenario in scenarios]
    if not ids:
        _fail("scenario document must contain at least one scenario")
    if len(ids) != len(set(ids)):
        _fail("scenario ids must be unique")
    return scenarios


def _metadata(dist: Distribution) -> bytes:
    """Render a distribution's core metadata."""
    lines = [
        "Metadata-Version: 2.3",
        f"Name: {dist.name}",
        f"Version: {dist.version}",
    ]
    if dist.requires_python is not None:
        lines.append(f"Requires-Python: {dist.requires_python}")
    lines.extend(f"Provides-Extra: {extra}" for extra in dist.provides_extra)
    lines.extend(f"Requires-Dist: {dependency}" for dependency in dist.dependencies)
    return ("\n".join(lines) + "\n\n").encode()


def _record_hash(data: bytes) -> str:
    """Return a RECORD hash entry in the urlsafe base64 form PEP 427 specifies."""
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + digest.decode()


def _zip_member(name: str, data: bytes) -> tuple[zipfile.ZipInfo, bytes]:
    """Describe one archive member with every host-dependent field pinned."""
    info = zipfile.ZipInfo(name, _ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info, data


def _wheel(dist: Distribution) -> tuple[str, bytes, bytes]:
    """Build one wheel, returning its filename, archive bytes, and metadata bytes."""
    wheel_name = canonicalize_name(dist.name, validate=True).replace("-", "_")
    filename = f"{wheel_name}-{dist.version}-py3-none-any.whl"
    dist_info = f"{wheel_name}-{dist.version}.dist-info"

    metadata = _metadata(dist)
    wheel = (
        b"Wheel-Version: 1.0\n"
        b"Generator: nab-deterministic-smoke\n"
        b"Root-Is-Purelib: true\n"
        b"Tag: py3-none-any\n\n"
    )

    metadata_name = f"{dist_info}/METADATA"
    wheel_member = f"{dist_info}/WHEEL"
    record_name = f"{dist_info}/RECORD"
    record = (
        f"{metadata_name},{_record_hash(metadata)},{len(metadata)}\n"
        f"{wheel_member},{_record_hash(wheel)},{len(wheel)}\n"
        f"{record_name},,\n"
    ).encode()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for member_name, contents in (
            (metadata_name, metadata),
            (wheel_member, wheel),
            (record_name, record),
        ):
            archive.writestr(*_zip_member(member_name, contents))
    return filename, buffer.getvalue(), metadata


def fixture_digest(root: Path) -> str:
    """Hash fixture paths and contents in a path-independent order."""
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode()
        contents = path.read_bytes()

        # Length-prefixed so no two trees can hash alike by shifting a byte from
        # one field into the next.
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    """Return the SHA-256 digest of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_hash(value: object) -> str:
    """Hash a JSON-representable value under a single canonical encoding."""
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _fixture_storage_manifest(root: Path) -> tuple[list[dict[str, object]], int]:
    """Record the stat of every entry under a resolved fixture tree.

    Entries are visited depth-first with children sorted by name, so the manifest
    for a given tree is byte-identical run to run. Anything that is not a regular
    file or a directory is rejected rather than described: an unreadable entry or
    a symlink would leave the fixture's real bytes outside the manifest.
    """
    pending = [root]
    records: list[dict[str, object]] = []
    file_count = 0
    while pending:
        path = pending.pop()
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError as exc:
            _fail(f"cannot stat fixture entry {path}: {exc}")

        mode = metadata.st_mode
        if stat.S_ISLNK(mode):
            _fail(f"fixture entry {path} must not be a symbolic link")
        if stat.S_ISDIR(mode):
            kind = "directory"
            try:
                children = sorted(path.iterdir(), key=lambda item: item.name)
            except OSError as exc:
                _fail(f"cannot list fixture directory {path}: {exc}")
            pending.extend(reversed(children))
        elif stat.S_ISREG(mode):
            kind = "file"
            file_count += 1
        else:
            _fail(f"fixture entry {path} is not a regular file or directory")

        relative = "." if path == root else path.relative_to(root).as_posix()
        records.append(
            {
                "path": relative,
                "kind": kind,
                "st_dev": metadata.st_dev,
                "st_ino": metadata.st_ino,
                "mode": stat.S_IMODE(mode),
                "size": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "ctime_ns": metadata.st_ctime_ns,
                "nlink": metadata.st_nlink,
            }
        )
    return records, file_count


def fixture_access_identity(
    root: Path,
    digest: str,
    *,
    mode: str,
) -> dict[str, object]:
    """Identify fixture bytes and the exact storage they were read from.

    Two trees holding identical bytes are deliberately not identical here: the
    resolved path and the stat manifest both take part. That is what lets the
    suite tell "the fixture never moved" apart from "something recreated the same
    bytes underneath us". Symlinked aliases of one tree do compare equal, because
    the root is resolved first.
    """
    if mode not in {"caller-materialized", "ephemeral-generated"}:
        _fail(f"unsupported fixture access mode {mode!r}")

    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        _fail(f"fixture root {root} does not exist: {exc}")
    if not resolved.is_dir():
        _fail(f"fixture root {resolved} is not a directory")

    records, file_count = _fixture_storage_manifest(resolved)
    if file_count == 0:
        _fail(f"fixture root {resolved} is empty")

    root_record = records[0]
    return {
        "schema": 1,
        "mode": mode,
        "resolved_root": str(resolved),
        "digest": digest,
        "st_dev": root_record["st_dev"],
        "st_ino": root_record["st_ino"],
        "entry_count": len(records),
        "file_count": file_count,
        "stat_manifest_sha256": _canonical_hash(records),
    }


def validate_materialized_fixture(
    root: Path,
    expected_digest: str,
    *,
    mode: str,
) -> tuple[Path, str, dict[str, object]]:
    """Validate an existing fixture and return its stable storage identity."""
    before = fixture_access_identity(root, expected_digest, mode=mode)
    resolved = Path(str(before["resolved_root"]))
    try:
        actual = fixture_digest(resolved)
    except OSError as exc:
        _fail(f"cannot read materialized fixture {resolved}: {exc}")

    # Bracket the read: a digest taken from a tree that shifted mid-read describes
    # neither the tree it started on nor the one it ended on.
    after = fixture_access_identity(resolved, actual, mode=mode)
    comparable_before = {**before, "digest": actual}
    if after != comparable_before:
        _fail("fixture storage changed while it was being validated")

    if actual != expected_digest:
        _fail(
            f"materialized fixture {resolved} has digest {actual},"
            f" expected {expected_digest}"
        )
    return resolved, actual, after


def _materialize_distribution(
    packages_dir: Path, dist: Distribution
) -> tuple[str, str]:
    """Write one wheel and its detached metadata, returning its Simple-API link."""
    canonical = str(canonicalize_name(dist.name, validate=True))
    filename, wheel, metadata = _wheel(dist)
    wheel_hash = hashlib.sha256(wheel).hexdigest()
    metadata_hash = hashlib.sha256(metadata).hexdigest()

    (packages_dir / filename).write_bytes(wheel)
    (packages_dir / f"{filename}.metadata").write_bytes(metadata)

    # Advertising data-core-metadata keeps resolves on the detached METADATA file
    # instead of pulling whole wheels, which is what the live index does.
    attributes = [f'data-core-metadata="sha256={metadata_hash}"']
    if dist.requires_python is not None:
        requires_python = html.escape(dist.requires_python, quote=True)
        attributes.append(f'data-requires-python="{requires_python}"')
    link = (
        f'<a href="../packages/{filename}#sha256={wheel_hash}" '
        f"{' '.join(attributes)}>{filename}</a>"
    )
    return canonical, link


def materialize_fixture(
    root: Path,
    distributions: Sequence[Distribution],
    expected_digest: str,
) -> str:
    """Materialize the local Simple index, or accept one already at this path.

    Reusing a populated root is what makes the fixture cheap to share across
    scenarios and test cases; it is only reused if its digest already matches.
    """
    try:
        root.mkdir(parents=True, exist_ok=True)
        populated = any(root.iterdir())
    except OSError as exc:
        _fail(f"cannot prepare fixture root {root}: {exc}")
    if populated:
        _resolved, actual, _access = validate_materialized_fixture(
            root,
            expected_digest,
            mode="caller-materialized",
        )
        return actual

    # Sorted so the listings, and therefore the digest, do not depend on manifest
    # ordering.
    packages_dir = root / "packages"
    packages_dir.mkdir()
    listings: dict[str, list[str]] = {}
    for dist in sorted(
        distributions,
        key=lambda item: (canonicalize_name(item.name, validate=True), item.version),
    ):
        canonical, link = _materialize_distribution(packages_dir, dist)
        listings.setdefault(canonical, []).append(link)

    root_links: list[str] = []
    for canonical, links in sorted(listings.items()):
        package_dir = root / canonical
        package_dir.mkdir()
        (package_dir / "index.html").write_text(
            "\n".join(links) + "\n", encoding="utf-8", newline="\n"
        )
        root_links.append(f'<a href="{canonical}/">{canonical}</a>')
    (root / "index.html").write_text(
        "\n".join(root_links) + "\n", encoding="utf-8", newline="\n"
    )

    _resolved, actual, _access = validate_materialized_fixture(
        root,
        expected_digest,
        mode="caller-materialized",
    )
    return actual


def _pins(result: ResolveResult) -> dict[str, dict[str, str]]:
    """Project a resolve into canonical name/version pins per successful target."""
    return {
        target_result.target.label: {
            str(canonicalize_name(name, validate=True)): str(version)
            for name, version in target_result.pins.items()
        }
        for target_result in result.target_results
        if target_result.success
    }


def _expected(scenario: Scenario) -> dict[str, dict[str, str]]:
    """Project a scenario's declared pins into the same shape as `_pins`."""
    return {
        item.target: {
            str(canonicalize_name(name, validate=True)): version
            for name, version in item.pins.items()
        }
        for item in scenario.expected
    }


def _fixture_distribution_map(
    distributions: Sequence[Distribution],
) -> dict[tuple[str, str], Distribution]:
    """Index the fixture manifest by canonical name and version."""
    return {
        (str(canonicalize_name(dist.name, validate=True)), dist.version): dist
        for dist in distributions
    }


def _fixture_wheel_inventory(
    index_root: Path,
) -> dict[tuple[str, str], tuple[Path, str]]:
    """Index the wheels actually on disk by their resolved path and digest.

    This is read back from the tree rather than derived from the manifest, so a
    lock is checked against the bytes a resolve could really have fetched.
    """
    inventory: dict[tuple[str, str], tuple[Path, str]] = {}
    for wheel in sorted((index_root / "packages").glob("*.whl")):
        stem = wheel.name.removesuffix("-py3-none-any.whl")
        try:
            raw_name, version = stem.rsplit("-", 1)
        except ValueError:
            _fail(f"fixture contains malformed wheel {wheel.name!r}")
        key = (
            str(canonicalize_name(raw_name.replace("_", "-"), validate=True)),
            version,
        )
        if key in inventory:
            _fail(f"fixture contains duplicate wheel artifact for {key}")
        inventory[key] = (wheel.resolve(), file_sha256(wheel))
    return inventory


def _artifact_location(value: str, lock_dir: Path) -> Path:
    """Resolve a lock's wheel reference, which may be a path or a `file:` URL.

    A relative path is taken against the lock directory, matching PEP 751. Any
    other scheme, or a `file:` URL naming a remote host, means the lock points
    outside the fixture and is rejected.
    """
    path = Path(value)
    if path.is_absolute():
        return path.resolve()

    parsed = urlparse(value)
    if parsed.scheme:
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            _fail(f"lock artifact is not a fixture file URL: {value!r}")
        return Path(url2pathname(parsed.path)).resolve()
    return (lock_dir / path).resolve()


def _marker_applies(
    marker: object, environment: Mapping[str, str], extras: set[str]
) -> bool:
    """Evaluate a marker against a target, once per extra the parent activates.

    A package pulled in for several extras contributes an edge if any one of them
    admits it. With no extras active the marker still has to see `extra` bound, so
    the empty string stands in.
    """
    if marker is None:
        return True
    parsed = marker if isinstance(marker, Marker) else Marker(str(marker))
    candidates = extras or {""}
    return any(parsed.evaluate({**environment, "extra": extra}) for extra in candidates)


def _expected_fixture_edges(
    selected: Mapping[str, str],
    environment: Mapping[str, str],
    requirements: Sequence[Requirement],
    fixture: Mapping[tuple[str, str], Distribution],
) -> dict[str, set[str]]:
    """Derive the dependency edges a lock should carry, straight from the fixture.

    Root requirements seed the active extras; walking the graph can activate more,
    and a newly active extra can open edges already walked past, so the walk
    repeats until neither the edges nor the extras grow.
    """
    extras: dict[str, set[str]] = {name: set() for name in selected}
    for requirement in requirements:
        name = str(canonicalize_name(requirement.name, validate=True))
        if name in selected and _marker_applies(requirement.marker, environment, set()):
            extras[name].update(requirement.extras)

    edges: dict[str, set[str]] = {name: set() for name in selected}
    changed = True
    while changed:
        changed = False
        for parent, version in selected.items():
            distribution = fixture.get((parent, version))
            if distribution is None:
                _fail(f"lock selected non-fixture package {(parent, version)}")
            for raw_dependency in distribution.dependencies:
                dependency = Requirement(raw_dependency)
                if not _marker_applies(dependency.marker, environment, extras[parent]):
                    continue
                child = str(canonicalize_name(dependency.name, validate=True))
                if child not in selected:
                    _fail(f"fixture edge {parent!r} references unselected {child!r}")
                edges[parent].add(child)
                before = len(extras[child])
                extras[child].update(dependency.extras)
                changed |= len(extras[child]) != before
    return edges


def _validate_lock_environments(
    lock: Pylock,
    result: ResolveResult,
    config: NabProjectConfig,
    requirements: Sequence[Requirement],
    fixture: Mapping[tuple[str, str], Distribution],
) -> None:
    """Check the lock's environments describe the resolved targets exactly.

    The declaration for a target is built from the markers actually in play for
    it, so it is only as wide as the roots, constraints, and selected dependencies
    make it. Each declaration must then admit its own target cell and no other.
    """
    input_requirements = [
        *requirements,
        *(Requirement(text) for text in config.constraints),
    ]

    expected_environments: set[str] = set()
    for item in result.target_results:
        if not item.success:
            continue
        domain_markers = {
            requirement.marker
            for requirement in input_requirements
            if requirement.marker is not None
        }
        for name, version in item.pins.items():
            distribution = fixture.get(
                (str(canonicalize_name(name, validate=True)), str(version))
            )
            if distribution is None:
                _fail(f"lock selected non-fixture package {(name, version)}")
            domain_markers.update(
                dependency.marker
                for raw_dependency in distribution.dependencies
                if (dependency := Requirement(raw_dependency)).marker is not None
            )
        expected_environments.add(
            str(Marker(environment_declaration(item.target, domain_markers)))
        )

    actual_environments = {str(marker) for marker in lock.environments or ()}
    if actual_environments != expected_environments or len(
        lock.environments or ()
    ) != len(expected_environments):
        _fail("emitted lock environments differ from the exact target cells")

    successful_targets = [item for item in result.target_results if item.success]
    expected_cell_coverage = {
        frozenset((item.target.label,)) for item in successful_targets
    }
    actual_cell_coverage = {
        frozenset(
            item.target.label
            for item in successful_targets
            if marker.evaluate(item.target.marker_env)
        )
        for marker in lock.environments or ()
    }
    if actual_cell_coverage != expected_cell_coverage:
        _fail("emitted lock environments do not cover each exact target cell once")


def _validate_lock_artifacts(
    lock: Pylock,
    result: ResolveResult,
    fixture: Mapping[tuple[str, str], Distribution],
    index_root: Path,
) -> None:
    """Check every locked package against the wheel the fixture really publishes.

    A lock that names the right versions can still be wrong about where they come
    from, so this pins the index, the artifact path, its hash, and the metadata
    the fixture declared, and rejects any sdist or direct source.
    """
    inventory = _fixture_wheel_inventory(index_root)
    expected_records = {
        (str(canonicalize_name(name, validate=True)), str(version))
        for target in result.target_results
        if target.success
        for name, version in target.pins.items()
    }

    actual_records: set[tuple[str, str]] = set()
    for package in lock.packages:
        if package.version is None:
            _fail(f"lock package {package.name!s} is unversioned")
        key = (
            str(canonicalize_name(str(package.name), validate=True)),
            str(package.version),
        )
        if key in actual_records:
            _fail(f"lock emitted duplicate artifact records for {key}")
        if key not in fixture or key not in inventory:
            _fail(f"lock selected non-fixture package {key}")

        # The fixture publishes wheels only, so any other source is a defect in
        # the lock rather than a property of the input.
        if any(
            value is not None
            for value in (
                package.vcs,
                package.directory,
                package.archive,
                package.sdist,
            )
        ):
            _fail(f"lock package {key} uses a direct source or sdist")
        if package.index != index_root.resolve().as_uri():
            _fail(f"lock package {key} uses a non-fixture index")

        wheels = tuple(package.wheels or ())
        if len(wheels) != 1:
            _fail(f"lock package {key} does not select exactly one fixture wheel")
        wheel = wheels[0]
        if (wheel.url is None) == (wheel.path is None):
            _fail(f"lock package {key} must have exactly one wheel URL or path")

        # Hashing the file at the recorded location catches a lock that quotes the
        # right digest beside the wrong artifact.
        location = _artifact_location(str(wheel.url or wheel.path), index_root)
        expected_path, expected_digest = inventory[key]
        if location != expected_path or wheel.filename != expected_path.name:
            _fail(f"lock package {key} wheel is outside the fixture")
        if wheel.hashes.get("sha256") != expected_digest or file_sha256(location) != (
            expected_digest
        ):
            _fail(f"lock package {key} wheel SHA-256 is invalid")

        distribution = fixture[key]
        expected_package_python = (
            SpecifierSet(distribution.requires_python)
            if distribution.requires_python is not None
            else None
        )
        if package.requires_python != expected_package_python:
            _fail(f"lock package {key} changed Requires-Python metadata")

        actual_records.add(key)

    if actual_records != expected_records:
        _fail("emitted lock artifact records differ from selected fixture records")


def _project_lock_targets(
    lock: Pylock,
    result: ResolveResult,
    requirements: Sequence[Requirement],
    fixture: Mapping[tuple[str, str], Distribution],
) -> dict[str, dict[str, str]]:
    """Read each target's install set back out of the single emitted lock.

    This is the consumer's view: apply the package markers for one target and see
    what remains. It has to name one version per package and reproduce the edges
    the fixture implies, or the lock does not describe the resolve it came from.
    """
    projected: dict[str, dict[str, str]] = {}
    for target_result in result.target_results:
        environment = target_result.target.marker_env

        selected: dict[str, str] = {}
        selected_packages: dict[str, Package] = {}
        for package in lock.packages:
            if package.marker is not None and not package.marker.evaluate(environment):
                continue
            name = str(canonicalize_name(str(package.name), validate=True))
            if name in selected:
                _fail(f"{target_result.target.label}: lock selects {name!r} twice")
            selected[name] = str(package.version)
            selected_packages[name] = package

        expected_edges = _expected_fixture_edges(
            selected, environment, requirements, fixture
        )

        # PEP 751 dependency entries carry a name and nothing else; a version or
        # source here would be a second, unreconciled copy of the resolution.
        actual_edges: dict[str, set[str]] = {name: set() for name in selected}
        for parent, package in selected_packages.items():
            for dependency in package.dependencies or ():
                if not isinstance(dependency, dict) or set(dependency) != {"name"}:
                    _fail(
                        f"{target_result.target.label}: lock dependency must contain"
                        " exactly one name field"
                    )
                raw_name = dependency.get("name")
                if not isinstance(raw_name, str):
                    _fail(f"{target_result.target.label}: malformed lock dependency")
                child = str(canonicalize_name(raw_name, validate=True))
                if child not in selected:
                    _fail(f"{target_result.target.label}: lock edge escapes selection")
                actual_edges[parent].add(child)

        if actual_edges != expected_edges:
            _fail(
                f"{target_result.target.label}: emitted lock edges differ:"
                f" expected {expected_edges}, got {actual_edges}"
            )
        projected[target_result.target.label] = selected

    return projected


def validate_nab_lock(
    lock: Pylock,
    result: ResolveResult,
    config: NabProjectConfig,
    requirements: Sequence[Requirement],
    distributions: Sequence[Distribution],
    index_root: Path,
) -> dict[str, dict[str, str]]:
    """Validate Nab's emitted lock against the fixture contract.

    Returns the lock's per-target install sets so the caller can compare them with
    the resolve's own pins.
    """
    fixture = _fixture_distribution_map(distributions)

    if str(lock.lock_version) != LOCK_VERSION or lock.created_by != "nab":
        _fail("emitted lock has the wrong lock version or creator")
    expected_requires_python = SpecifierSet(config.requires_python or "")
    if lock.requires_python != expected_requires_python:
        _fail("emitted lock changed the declared Python domain")

    _validate_lock_environments(lock, result, config, requirements, fixture)
    _validate_lock_artifacts(lock, result, fixture, index_root)
    return _project_lock_targets(lock, result, requirements, fixture)


def _lock_projection(
    result: ResolveResult,
    config: NabProjectConfig,
    requirements: Sequence[Requirement],
    distributions: Sequence[Distribution],
    index_root: Path,
) -> dict[str, dict[str, str]]:
    """Emit the lock for one resolve and validate it end to end."""
    lock_input = build_lock_input(result, config=config)
    lock = build_pylock(lock_input, lock_dir=index_root)
    lock.validate()
    return validate_nab_lock(
        lock,
        result,
        config,
        requirements,
        distributions,
        index_root,
    )


def _search_signature(result: ResolveResult) -> tuple[_TargetSearch, ...]:
    """Capture the search counters that must match across repeated runs."""
    return tuple(
        _TargetSearch(
            target=target_result.target.label,
            decisions=target_result.decisions,
            rounds=target_result.rounds,
            conflicts=target_result.conflicts,
            backjumps=target_result.backjumps,
        )
        for target_result in result.target_results
    )


def prepare_scenario(scenario: Scenario, index_root: Path) -> PreparedScenario:
    """Build the effective targets, resolver policy, and parsed root inputs.

    Everything a timed interval would otherwise pay for happens here: marker
    parsing, matrix expansion, and policy resolution all land outside the clock.
    """
    targets = Matrix(
        python=scenario.python,
        platforms=tuple(PlatformSpec(platform) for platform in scenario.platforms),
    ).expand()

    index = IndexConfig("smoke", index_root.resolve().as_uri())
    config = NabProjectConfig(
        constraints=scenario.constraints,
        requires_python=scenario.python,
        indexes=(index,),
    )
    if scenario.resolution is not None:
        config = replace(config, resolution=scenario.resolution)

    build_policy = enforce_build_policy_for_targets(
        targets=targets,
        build_policy=config.build_policy,
        build_policy_set=False,
        package_overrides=config.package_overrides,
        index_overrides=config.index_overrides,
    )
    config = replace(config, build_policy=build_policy)

    expected = _expected(scenario)
    labels = {target.label for target in targets}
    if labels != set(expected):
        _fail(
            f"{scenario.id}: expected targets {sorted(expected)},"
            f" matrix expands to {sorted(labels)}"
        )
    return PreparedScenario(
        scenario=scenario,
        targets=tuple(targets),
        index=index,
        config=config,
        requirements=tuple(Requirement(text) for text in scenario.requirements),
        expected=expected,
        align_across_targets=(
            True
            if scenario.align_across_targets is None
            else scenario.align_across_targets
        ),
    )


def _fixture_listing_packages(distributions: Sequence[Distribution]) -> tuple[str, ...]:
    """Every canonical name in the fixture, in a stable order."""
    return tuple(sorted({canonicalize_name(dist.name) for dist in distributions}))


def _await_listings(coordinator: FetchCoordinator, packages: Sequence[str]) -> None:
    """Land every fixture listing before the caller starts timing.

    The priority scan sorts a package whose listing is still in flight behind
    the ready ones, so a resolve racing its own fetches can decide in a
    different order run to run and reach the same pins by a different path.
    Requesting the whole fixture up front and waiting takes that race out of
    the measurement; it does not change what the resolver does with a listing
    once it holds one.  Every request goes out before the first wait so they
    still overlap.
    """
    for event in [coordinator.request_listing(package) for package in packages]:
        event.wait()


def _resolve_once(
    prepared: PreparedScenario, listing_packages: Sequence[str]
) -> tuple[ResolveResult, int]:
    """Run one fresh-coordinator resolve and time only the resolver call."""
    with FetchCoordinator(
        Urllib3AsyncTransport(),
        indexes=[prepared.index],
        offline=True,
    ) as coordinator:
        arguments = (
            coordinator,
            prepared.targets,
            prepared.requirements,
        )
        _await_listings(coordinator, listing_packages)
        start = time.perf_counter_ns()

        # Omitting the argument entirely is the only way to measure the shipped
        # default; passing its current value would pin the test to today's choice.
        if prepared.scenario.align_across_targets is None:
            result = resolve_with_coordinator(*arguments, config=prepared.config)
        else:
            result = resolve_with_coordinator(
                *arguments,
                config=prepared.config,
                align_across_targets=prepared.scenario.align_across_targets,
            )
        elapsed = time.perf_counter_ns() - start
    return result, elapsed


def _validate_observation(
    prepared: PreparedScenario,
    result: ResolveResult,
    distributions: Sequence[Distribution],
    index_root: Path,
) -> tuple[
    dict[str, dict[str, str]],
    dict[str, dict[str, str]] | None,
    dict[str, dict[str, object]],
]:
    """Validate one result and return its stable semantic projection.

    An unsatisfiable case has to fail the way the resolver promises to fail: a
    proof-bearing error on every target, with no pins and no lock left behind.
    """
    scenario = prepared.scenario
    if scenario.outcome == "unsatisfiable":
        unexpected = [
            target_result.target.label
            for target_result in result.target_results
            if (
                target_result.success
                or target_result.pins
                or target_result.lock is not None
                or not isinstance(target_result.error, ResolutionError)
                or target_result.error.incompatibility is None
            )
        ]
        if unexpected:
            _fail(
                f"{scenario.id}: expected a proof-bearing ResolutionError, no pins,"
                " and no lock for every target,"
                f" got unexpected results for {unexpected}"
            )

        actual = {
            target_result.target.label: {} for target_result in result.target_results
        }
        if actual != prepared.expected:
            _fail(
                f"{scenario.id}: failure targets differ: expected"
                f" {prepared.expected}, got {actual}"
            )

        failures = {
            target_result.target.label: {
                "type": type(target_result.error).__name__,
                "message": str(target_result.error),
                "has_incompatibility": True,
            }
            for target_result in result.target_results
        }
        return actual, None, failures

    failures = [
        f"{target_result.target.label}: {target_result.error}"
        for target_result in result.target_results
        if not target_result.success
    ]
    if failures:
        _fail(f"{scenario.id}: resolve failed: {failures}")

    actual = _pins(result)
    if actual != prepared.expected:
        _fail(f"{scenario.id}: pins differ: expected {prepared.expected}, got {actual}")

    projected = _lock_projection(
        result,
        prepared.config,
        prepared.requirements,
        distributions,
        index_root,
    )
    if projected != prepared.expected:
        _fail(
            f"{scenario.id}: lock projection differs: expected"
            f" {prepared.expected}, got {projected}"
        )
    return actual, projected, {}


def _observe_scenario(
    prepared: PreparedScenario,
    distributions: Sequence[Distribution],
    index_root: Path,
    listing_packages: Sequence[str],
) -> _ScenarioObservation:
    """Run and fully validate one resolve."""
    result, elapsed = _resolve_once(prepared, listing_packages)
    pins, lock_projection, failures = _validate_observation(
        prepared, result, distributions, index_root
    )
    fetch = tuple(
        (
            target_result.target.label,
            target_result.distributions_seen,
            target_result.metadata_fetched,
        )
        for target_result in result.target_results
    )
    return _ScenarioObservation(
        elapsed_ns=elapsed,
        pins=pins,
        lock_projection=lock_projection,
        failures=failures,
        search=_search_signature(result),
        fetch=fetch,
    )


def _validate_repeatability(
    scenario: Scenario,
    observations: Sequence[_ScenarioObservation],
    distributions_seen: Mapping[str, Sequence[int]],
    metadata_fetched: Mapping[str, Sequence[int]],
) -> None:
    """Require every repeat of a scenario to agree, not just the first and last.

    Timing may drift, but a search that reaches the same answer by a different
    path, or that visits a different number of distributions, is not the same
    measurement and cannot be compared with the runs around it.
    """
    first = observations[0]
    if any(observation.search != first.search for observation in observations[1:]):
        _fail(f"{scenario.id}: resolver search counters varied across repeated runs")
    if any(
        (
            observation.pins,
            observation.lock_projection,
            observation.failures,
        )
        != (first.pins, first.lock_projection, first.failures)
        for observation in observations[1:]
    ):
        _fail(f"{scenario.id}: semantic output varied across repeated runs")
    if any(observation.fetch != first.fetch for observation in observations[1:]):
        _fail(f"{scenario.id}: fetch counters varied across repeated runs")
    for label in sorted(distributions_seen):
        if (
            len(set(distributions_seen[label])) != 1
            or len(set(metadata_fetched[label])) != 1
        ):
            _fail(
                f"{scenario.id}: fetch counters varied across repeated runs for {label}"
            )


def _search_report(
    signature: Sequence[_TargetSearch],
    distributions_seen: Mapping[str, Sequence[int]],
    metadata_fetched: Mapping[str, Sequence[int]],
) -> tuple[dict[str, int], list[dict[str, object]]]:
    """Summarize search and fetch counters in total and per target."""
    totals = {
        "decisions": sum(row.decisions for row in signature),
        "rounds": sum(row.rounds for row in signature),
        "conflicts": sum(row.conflicts for row in signature),
        "backjumps": sum(row.backjumps for row in signature),
    }
    per_target = [
        {
            "target": row.target,
            "decisions": row.decisions,
            "rounds": row.rounds,
            "conflicts": row.conflicts,
            "backjumps": row.backjumps,
            "distributions_seen": distributions_seen[row.target],
            "metadata_fetched": metadata_fetched[row.target],
        }
        for row in signature
    ]
    return totals, per_target


def _timing_report(
    scenario: Scenario, inner_walls: list[list[int]]
) -> dict[str, object] | None:
    """Summarize batched timings, or return None for the untimed semantic lane.

    Raw intervals are kept alongside the aggregates so a reader can see the batch
    a summary came from rather than take the summary on faith.
    """
    if scenario.lane != "performance":
        return None
    aggregate_walls = [sum(sample) for sample in inner_walls]
    return {
        "aggregate_samples": aggregate_walls,
        "raw_inner_samples": inner_walls,
        "median": statistics.median(aggregate_walls),
        "minimum": min(aggregate_walls),
        "maximum": max(aggregate_walls),
    }


def _scenario_report(
    scenario: Scenario,
    prepared: PreparedScenario,
    observations: Sequence[_ScenarioObservation],
    distributions_seen: Mapping[str, Sequence[int]],
    metadata_fetched: Mapping[str, Sequence[int]],
    inner_walls: list[list[int]],
    sample_count: int,
    batch_size: int,
) -> dict[str, Any]:
    """Assemble the JSON record for one scenario."""
    final = observations[-1]
    search, search_per_target = _search_report(
        observations[0].search,
        distributions_seen,
        metadata_fetched,
    )
    return {
        "id": scenario.id,
        "lane": scenario.lane,
        "outcome": scenario.outcome,
        "mode": scenario.mode,
        "provenance": scenario.provenance,
        "purpose": scenario.purpose,
        "resolution": prepared.config.resolution.value,
        "build_policy": prepared.config.build_policy.value,
        "align_across_targets": prepared.align_across_targets,
        "targets": sorted(prepared.expected),
        "sample_count": sample_count,
        "warmup_batches": scenario.warmups,
        "batch_size": batch_size,
        "measured_resolves": sample_count * batch_size,
        "search": search,
        "search_per_target": search_per_target,
        "pins_per_target": final.pins,
        "lock_projection_per_target": final.lock_projection,
        "failures_per_target": final.failures,
        "lock_validation": (
            "exact PEP 751 domain, fixture sources, wheels, hashes, and edges"
            if scenario.outcome == "satisfiable"
            else None
        ),
        "wall_time_ns": _timing_report(scenario, inner_walls),
    }


def run_scenario(scenario: Scenario, index_root: Path, runs: int) -> dict[str, Any]:
    """Validate one contract and, for performance cases, collect batched samples."""
    if runs < 1:
        _fail(f"{scenario.id}: runs must be at least 1")
    _validate_asyncio_wakeup_transport()

    # Expectations come from the shipped manifest, not from whatever tree
    # index_root happens to hold; the digest check is what ties the two together.
    prepared = prepare_scenario(scenario, index_root)
    distributions, _fixture_manifest_digest = load_fixture()
    listing_packages = _fixture_listing_packages(distributions)

    # A semantic case is validated once and never timed, so it collapses to a
    # single unmeasured batch of one.
    sample_count = runs if scenario.lane == "performance" else 0
    measurement_batches = sample_count if scenario.lane == "performance" else 1
    batch_size = scenario.batch_size if scenario.lane == "performance" else 1

    inner_walls: list[list[int]] = []
    observations: list[_ScenarioObservation] = []
    distributions_seen_per_target: dict[str, list[int]] = {
        target: [] for target in sorted(prepared.expected)
    }
    metadata_fetched_per_target: dict[str, list[int]] = {
        target: [] for target in sorted(prepared.expected)
    }

    for _ in range(scenario.warmups):
        observations.extend(
            _observe_scenario(prepared, distributions, index_root, listing_packages)
            for _ in range(batch_size)
        )

    for _ in range(measurement_batches):
        sample: list[int] = []
        for _ in range(batch_size):
            observation = _observe_scenario(
                prepared, distributions, index_root, listing_packages
            )
            observations.append(observation)
            sample.append(observation.elapsed_ns)
            for label, seen, fetched in observation.fetch:
                distributions_seen_per_target[label].append(seen)
                metadata_fetched_per_target[label].append(fetched)
        if scenario.lane == "performance":
            inner_walls.append(sample)

    _validate_repeatability(
        scenario,
        observations,
        distributions_seen_per_target,
        metadata_fetched_per_target,
    )
    return _scenario_report(
        scenario,
        prepared,
        observations,
        distributions_seen_per_target,
        metadata_fetched_per_target,
        inner_walls,
        sample_count,
        batch_size,
    )


def run_suite(
    index_root: Path,
    scenarios: Sequence[Scenario],
    runs: int,
    fixture_sha256: str,
    *,
    fixture_mode: str = "caller-materialized",
) -> dict[str, Any]:
    """Run selected scenarios in manifest order against one shared fixture."""
    index_root, fixture_sha256, fixture_access = validate_materialized_fixture(
        index_root,
        fixture_sha256,
        mode=fixture_mode,
    )

    results = [run_scenario(scenario, index_root, runs) for scenario in scenarios]

    # Re-checking the identity, not just the digest, is what makes every earlier
    # result attributable to the same tree the suite started on.
    _resolved_after, fixture_sha256_after, fixture_access_after = (
        validate_materialized_fixture(
            index_root,
            fixture_sha256,
            mode=fixture_mode,
        )
    )
    if fixture_access_after != fixture_access:
        _fail("fixture storage changed while the suite ran")

    return {
        "schema": 1,
        "fixture_sha256": fixture_sha256,
        "fixture_sha256_after": fixture_sha256_after,
        "fixture_access": fixture_access,
        "fixture_access_after": fixture_access_after,
        "timing_boundary": TIMING_BOUNDARY,
        "cache_mode": CACHE_MODE,
        "scenarios": results,
    }


def _print_summary(report: Mapping[str, Any]) -> None:
    """Print one line per scenario; the JSON report carries the rest."""
    print(
        "scenario                             lane         targets  decisions"
        " conflicts median_ms"
    )
    for result in report["scenarios"]:
        timing = result["wall_time_ns"]
        median = f"{timing['median'] / 1_000_000:.2f}" if timing else "-"
        print(
            f"{result['id']:<36}"
            f" {result['lane']:<11}"
            f" {len(result['targets']):>7}"
            f" {result['search']['decisions']:>10}"
            f" {result['search']['conflicts']:>9}"
            f" {median:>9}"
        )


def main() -> None:
    """Materialize the fixture and run the selected deterministic cases."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument(
        "--lane",
        choices=("all", "semantic", "performance"),
        default="all",
    )
    parser.add_argument("--json", type=Path)
    parser.add_argument("--fixture-dir", type=Path)
    parser.add_argument("--materialize-only", action="store_true")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    distributions, expected_digest = load_fixture()
    scenarios = load_scenarios()

    requested = set(args.scenario)
    known = {scenario.id for scenario in scenarios}
    unknown = requested - known
    if unknown:
        parser.error(f"unknown --scenario values: {sorted(unknown)}")

    # Filters narrow the manifest without reordering it, so a report's scenarios
    # always appear in declaration order.
    selected = [
        scenario
        for scenario in scenarios
        if (not requested or scenario.id in requested)
        and args.lane in {"all", scenario.lane}
    ]
    if not selected and not args.materialize_only:
        parser.error("the --scenario and --lane filters select no scenarios")

    # A caller-supplied directory is reused across invocations and outlives the
    # run; without one the fixture is built and discarded.
    if args.fixture_dir is not None:
        digest = materialize_fixture(args.fixture_dir, distributions, expected_digest)
        if args.materialize_only:
            print(f"{args.fixture_dir} {digest}")
            return
        report = run_suite(args.fixture_dir, selected, args.runs, digest)
    else:
        if args.materialize_only:
            parser.error("--materialize-only requires --fixture-dir")
        with tempfile.TemporaryDirectory(prefix="nab-smoke-") as temporary:
            fixture_dir = Path(temporary)
            digest = materialize_fixture(fixture_dir, distributions, expected_digest)
            report = run_suite(
                fixture_dir,
                selected,
                args.runs,
                digest,
                fixture_mode="ephemeral-generated",
            )

    _print_summary(report)
    if args.json is not None:
        args.json.write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n"
        )


if __name__ == "__main__":
    main()
