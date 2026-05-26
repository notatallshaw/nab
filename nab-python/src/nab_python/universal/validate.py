"""Validate wheel metadata consistency across the resolved lock.

After ``resolve_universal`` produces a :class:`UniversalResult`, this
module fetches the metadata of the specific wheel each tuple would
install for each pinned package and checks the wheel's
``Requires-Dist`` (after marker eval) against the deps the resolver
assumed. Two PyPI packages where wheels for one ``(name, version)``
disagree on deps drive the need: ``apache-beam`` (the win32 wheel
omits pyarrow that the linux wheels declare) and ``open3d``
(macos / linux / windows wheels diverge on addict, ipywidgets and
others). The matrix model bounds the cost to one extra metadata
fetch per ``(tuple, package)`` pair where the chosen wheel differs
from the resolver's baseline. See :class:`PinValidation` for the
per-pin status values and :meth:`ValidationReport.fatal_findings`
for the install-time fatality rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from nab_index.client import WheelFile

from .._vendor.packaging.requirements import (
    InvalidRequirement,
    Requirement,
)
from .._vendor.packaging.utils import canonicalize_name
from ..metadata import DEPENDENCY_FIELDS, load_static_project, parse_metadata
from .wheel_selection import select_wheel_for_tuple

if TYPE_CHECKING:
    from .._vendor.packaging.version import Version
    from ..fetch import FetchCoordinator
    from .matrix import MatrixTuple
    from .resolve import UniversalResult


__all__ = [
    "ExtraDiff",
    "PinValidation",
    "ValidationReport",
    "validate_lock",
]


# Statuses that always fail the lock at install time, regardless of
# build policy.
_ALWAYS_FATAL_STATUSES = frozenset({"no_compatible_wheel", "no_metadata"})

# Statuses that fail only when the build policy refuses to build
# from sdist (BuildPolicy.NEVER).  These pins resolve fine if the
# user has a build toolchain.
_BUILD_REQUIRED_STATUSES = frozenset({"sdist_only", "no_compatible_wheel_with_sdist"})

# Metadata-Version 2.2 introduced PEP 643's Dynamic field.  Earlier
# versions have no static-deps guarantee.
_MIN_STATIC_METADATA_VERSION = (2, 2)


@dataclass(frozen=True)
class ExtraDiff:
    """Per-extra divergence between baseline and chosen-wheel metadata.

    ``extra_deps`` are deps the chosen wheel declares for ``extra``
    that the baseline does not; ``missing_deps`` are the inverse.  An
    extra is included only when at least one of these is non-empty.
    """

    extra: str
    extra_deps: tuple[str, ...] = ()
    missing_deps: tuple[str, ...] = ()


@dataclass(frozen=True)
class PinValidation:
    """Result of validating one ``(tuple, package, version)`` pin.

    ``status`` is one of:

    - ``ok``: chosen wheel's deps (after marker eval) match the resolver's.
    - ``divergent``: wheel has metadata but deps differ from baseline.
    - ``sdist_only``: no wheels at all; the user must build from sdist.
      Fatal under ``BuildPolicy.NEVER``.
    - ``no_compatible_wheel``: wheels exist but none match the tuple's
      tags and no buildable sdist exists. Always fatal.
    - ``no_compatible_wheel_with_sdist``: as above but a sdist is
      available. Fatal under ``BuildPolicy.NEVER``.
    - ``no_metadata``: the chosen wheel has no fetchable metadata.
      Always fatal.
    - ``static_sdist_authoritative``: the sdist's PEP 621 or PEP 643
      metadata guarantees every wheel of this version shares the same
      dep-affecting metadata, so per-wheel fetches were skipped.
    """

    tuple_label: str
    package: str
    version: str
    status: str
    chosen_wheel: str | None = None
    detail: str = ""
    extra_deps: tuple[str, ...] = ()
    missing_deps: tuple[str, ...] = ()
    extras_divergent: tuple[ExtraDiff, ...] = ()


@dataclass
class ValidationReport:
    """Aggregate validation result for a UniversalResult."""

    pins_checked: int = 0
    pins_ok: int = 0
    findings: list[PinValidation] = field(default_factory=list)

    def fatal_findings(self, *, build_allowed: bool = False) -> list[PinValidation]:
        """Return findings that would prevent installation.

        Always fatal: ``no_compatible_wheel`` (no installable
        artifact) and ``no_metadata`` (we can't trust the resolver
        ran with the right deps).

        Fatal under BuildPolicy.NEVER: ``sdist_only`` and
        ``no_compatible_wheel_with_sdist`` (only sdist available).
        """
        return [
            f
            for f in self.findings
            if f.status in _ALWAYS_FATAL_STATUSES
            or (not build_allowed and f.status in _BUILD_REQUIRED_STATUSES)
        ]


def validate_lock(
    result: UniversalResult,
    coordinator: FetchCoordinator,
) -> ValidationReport:
    """Validate every pin in ``result`` against its per-tuple wheel metadata.

    ``coordinator`` is the same FetchCoordinator the resolver used.
    Re-using it keeps cached metadata warm.
    """
    report = ValidationReport()
    # ``pins_ok`` counts both per-wheel-validated successes and
    # static-sdist-authoritative pins; both mean "the lock is sound
    # for this pin", just established via different evidence.
    ok_statuses = {"ok", "static_sdist_authoritative"}
    for tr in result.tuple_results:
        if not tr.success:
            continue
        for pkg, version in tr.pins.items():
            finding = _validate_pin(coordinator, tr.tuple_, pkg, version)
            report.findings.append(finding)
            report.pins_checked += 1
            if finding.status in ok_statuses:
                report.pins_ok += 1
    return report


def _validate_pin(  # noqa: PLR0911 - one return per outcome reads cleaner here
    coordinator: FetchCoordinator,
    tup: MatrixTuple,
    package: str,
    version: Version,
) -> PinValidation:
    """Run the per-pin checks; emit a PinValidation outcome."""
    normalized = canonicalize_name(package)
    listing = coordinator.index.get_listing(normalized) or []
    files_at_version = [f for f in listing if f.version == str(version)]
    wheels_at_version = [f for f in files_at_version if isinstance(f, WheelFile)]
    has_sdist = any(not isinstance(f, WheelFile) for f in files_at_version)
    if not wheels_at_version:
        return PinValidation(
            tuple_label=tup.label,
            package=package,
            version=str(version),
            status="sdist_only",
            detail="no wheels at this version; install requires building from sdist",
        )
    chosen = select_wheel_for_tuple(
        wheels_at_version,
        python_version=tup.python_version,
        spec=tup.platform_spec,
        implementation=tup.implementation,
    )
    if chosen is None:
        status = (
            "no_compatible_wheel_with_sdist" if has_sdist else "no_compatible_wheel"
        )
        detail_suffix = (
            "; sdist available so build-from-source is possible"
            if has_sdist
            else "; no sdist either, install will fail"
        )
        return PinValidation(
            tuple_label=tup.label,
            package=package,
            version=str(version),
            status=status,
            detail=(
                f"{len(wheels_at_version)} wheels at this version but none "
                f"compatible with {tup.python_version}/{tup.platform_id}"
                + detail_suffix
            ),
        )
    # PEP 643 fast path: if the resolver's baseline metadata declares
    # all dependency-affecting fields static (no Dynamic), every wheel
    # built from the sdist MUST have the same Requires-Dist /
    # Provides-Extra.  Skip the per-wheel fetch.
    if _baseline_has_static_deps(coordinator, normalized, version):
        return PinValidation(
            tuple_label=tup.label,
            package=package,
            version=str(version),
            status="static_sdist_authoritative",
            chosen_wheel=chosen.filename,
            detail="PEP 643 static deps; all wheels guaranteed equal",
        )
    metadata_text = _fetch_wheel_metadata(coordinator, normalized, version, chosen)
    if metadata_text is None:
        return PinValidation(
            tuple_label=tup.label,
            package=package,
            version=str(version),
            status="no_metadata",
            chosen_wheel=chosen.filename,
            detail="wheel has no metadata file we could fetch",
        )
    chosen_by_extra = _evaluate_metadata_deps_by_extra(metadata_text, tup.environment)
    listing_text = coordinator.index.get_metadata(normalized, str(version))
    listing_by_extra: dict[str | None, set[str]] = (
        _evaluate_metadata_deps_by_extra(listing_text, tup.environment)
        if listing_text is not None
        else {None: set()}
    )
    chosen_base = chosen_by_extra.get(None, set())
    listing_base = listing_by_extra.get(None, set())
    extra = sorted(chosen_base - listing_base)
    missing = sorted(listing_base - chosen_base)
    extras_divergent = _per_extra_divergence(chosen_by_extra, listing_by_extra)

    if not extra and not missing and not extras_divergent:
        return PinValidation(
            tuple_label=tup.label,
            package=package,
            version=str(version),
            status="ok",
            chosen_wheel=chosen.filename,
        )
    if extra or missing:
        return PinValidation(
            tuple_label=tup.label,
            package=package,
            version=str(version),
            status="divergent",
            chosen_wheel=chosen.filename,
            detail=(
                f"chosen wheel differs from listing baseline: "
                f"+{len(extra)} extra, -{len(missing)} missing deps"
            ),
            extra_deps=tuple(extra),
            missing_deps=tuple(missing),
            extras_divergent=extras_divergent,
        )
    return PinValidation(
        tuple_label=tup.label,
        package=package,
        version=str(version),
        status="divergent_in_extra",
        chosen_wheel=chosen.filename,
        detail=(
            f"chosen wheel diverges in {len(extras_divergent)} "
            f"extra(s): {', '.join(d.extra for d in extras_divergent)}"
        ),
        extras_divergent=extras_divergent,
    )


def _per_extra_divergence(
    chosen: dict[str | None, set[str]],
    baseline: dict[str | None, set[str]],
) -> tuple[ExtraDiff, ...]:
    """Compare per-extra deps between chosen wheel and baseline.

    Each extra (in either dict, excluding ``None``) is a candidate.
    An extra is reported only when its chosen-vs-baseline diff is
    non-empty.  An extra present in only one side is still compared:
    the absent side contributes the empty set.
    """
    extras = {e for e in chosen.keys() | baseline.keys() if e is not None}
    diffs: list[ExtraDiff] = []
    for extra in sorted(extras):
        c = chosen.get(extra, set())
        b = baseline.get(extra, set())
        extra_deps = tuple(sorted(c - b))
        missing_deps = tuple(sorted(b - c))
        if extra_deps or missing_deps:
            diffs.append(
                ExtraDiff(extra=extra, extra_deps=extra_deps, missing_deps=missing_deps)
            )
    return tuple(diffs)


def _baseline_has_static_deps(
    coordinator: FetchCoordinator,
    normalized: str,
    version: Version,
) -> bool:
    """Return True if baseline declares deps fully static.

    Two qualifying routes:

    1. PEP 643: the METADATA is Version 2.2+ and ``Dynamic`` does not
       include ``Requires-Dist`` or ``Provides-Extra``. Every wheel
       built from this sdist must share those fields.
    2. PEP 621 pyproject.toml: the sdist contains a ``pyproject.toml``
       with a ``[project]`` table that defines ``dependencies`` (and
       ``optional-dependencies`` if used) statically (not listed in
       ``[project].dynamic``). Per PEP 621, build backends must honour
       the declared values. This route covers older Metadata (pre-2.2)
       and backends that mark fields Dynamic in METADATA even when
       pyproject.toml is static.

    Returns False when neither route qualifies.
    """
    if _metadata_is_pep643_static(coordinator, normalized, version):
        return True
    return _pyproject_is_pep621_static(coordinator, normalized, version)


def _metadata_is_pep643_static(
    coordinator: FetchCoordinator,
    normalized: str,
    version: Version,
) -> bool:
    """Route 1: PEP 643 Metadata 2.2+ without dependency Dynamic fields."""
    text = coordinator.index.get_metadata(normalized, str(version))
    if text is None:
        return False
    try:
        metadata = parse_metadata(text)
    except Exception:  # noqa: BLE001
        return False
    if metadata.metadata_version is None:
        return False
    try:
        major, minor = (int(p) for p in metadata.metadata_version.split(".", 1))
    except ValueError:
        return False
    if (major, minor) < _MIN_STATIC_METADATA_VERSION:
        return False
    return not (DEPENDENCY_FIELDS & metadata.dynamic)


def _pyproject_is_pep621_static(
    coordinator: FetchCoordinator,
    normalized: str,
    version: Version,
) -> bool:
    """Route 2: sdist pyproject.toml has static ``[project].dependencies``.

    The coordinator caches pyproject.toml text via
    :meth:`InMemoryIndex.get_sdist_pyproject` whenever it fetches
    an sdist.  For wheels-only resolves, no sdist fetch happens, so
    this returns False; callers fall through to the per-wheel
    validation path.

    PEP 621 says ``[project].dependencies`` is static UNLESS
    ``dependencies`` appears in ``[project].dynamic``.  When neither
    ``dependencies`` nor ``optional-dependencies`` sits in the
    dynamic set, the values in pyproject.toml are authoritative for
    every wheel built from this sdist.  The keys themselves may be
    absent: PEP 621 treats that as "no deps", which is itself
    static.
    """
    text = coordinator.index.get_sdist_pyproject(normalized, str(version))
    if text is None:
        return False
    return load_static_project(text) is not None


def _fetch_wheel_metadata(
    coordinator: FetchCoordinator,
    normalized: str,
    version: Version,
    wheel: WheelFile,
) -> str | None:
    """Fetch wheel-specific metadata via the coordinator's transport.

    Uses :meth:`FetchCoordinator.request_wheel_metadata` which submits
    through the same async fetcher the resolver uses, sharing
    connection pooling and the on-disk cache.  Cache key is
    ``(name, "<version>#<filename>")`` so the resolver-time
    ``(name, version)`` cache is undisturbed.

    Returns ``None`` if the wheel has no PEP 658 ``metadata_url`` or
    the fetch failed.
    """
    if wheel.metadata_url is None:
        return None
    event = coordinator.request_wheel_metadata(
        normalized, str(version), wheel.filename, wheel.metadata_url
    )
    event.wait()
    return coordinator.index.get_metadata(normalized, f"{version}#{wheel.filename}")


def _evaluate_metadata_deps_by_extra(
    metadata_text: str,
    environment: dict[str, str],
) -> dict[str | None, set[str]]:
    """Return deps grouped by extra: ``{None: base_deps, "extra": deps_for_extra}``.

    Each ``Requires-Dist`` is bucketed by which ``extra`` setting (if
    any) makes its marker evaluate True.  Markers without ``extra``
    references go to the base bucket; markers with ``extra ==
    "name"`` go to that named bucket.  This lets the validator catch
    per-extra divergence between the resolver's listing baseline and
    the chosen wheel, even when base deps match.
    """
    metadata = parse_metadata(metadata_text)
    extras = {canonicalize_name(e) for e in metadata.provides_extra}
    out: dict[str | None, set[str]] = {None: set()}
    for e in extras:
        out[e] = set()
    base_env = {**environment, "extra": ""}
    for req_text in metadata.requires_dist:
        try:
            req = Requirement(str(req_text))
        except InvalidRequirement:  # pragma: no cover
            continue
        name = canonicalize_name(req.name)
        marker = req.marker
        if marker is None or marker.evaluate(base_env):
            out[None].add(name)
            continue
        for e in extras:
            if marker.evaluate({**environment, "extra": e}):
                out[e].add(name)
    return out
