"""Tests for the wheel-metadata validation pass.

Uses an in-memory FetchCoordinator stub to inject deterministic
listings and per-wheel metadata, then asserts the validation
report classifies each pin correctly.

Network mocking note: ``_fetch_wheel_metadata`` opens an httpx
client to fetch wheel-specific metadata.  The tests here work
exclusively from already-cached metadata (placed in the in-memory
index under sentinel keys) so no real HTTP runs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from unittest.mock import MagicMock

from nab_index.client import SdistFile, WheelFile
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python._vendor.packaging.version import Version
from nab_python.universal.matrix import MatrixTuple
from nab_python.universal.resolve import TupleResult, UniversalResult
from nab_python.universal.validate import (
    PinValidation,
    ValidationReport,
    validate_lock,
)
from nab_python.universal.wheel_selection import PlatformSpec

_BASE_METADATA = (
    "Metadata-Version: 2.1\n"
    "Name: pkg\n"
    "Version: 1.0\n"
    "Requires-Dist: requests>=2.0\n"
    "Requires-Dist: numpy>=1.0\n"
    "\n"
)

_DIVERGENT_METADATA = (
    "Metadata-Version: 2.1\n"
    "Name: pkg\n"
    "Version: 1.0\n"
    "Requires-Dist: requests>=2.0\n"
    "Requires-Dist: ipywidgets>=8.0\n"
    "\n"
)

# Metadata 2.2 with Requires-Dist marked Dynamic.  Defeats the
# PEP 643 static-deps fast path so per-wheel validation runs.
_DYNAMIC_DEPS_METADATA = (
    "Metadata-Version: 2.2\n"
    "Name: pkg\n"
    "Version: 1.0\n"
    "Dynamic: Requires-Dist\n"
    "Requires-Dist: requests>=2.0\n"
    "Requires-Dist: numpy>=1.0\n"
    "\n"
)

# Metadata 2.2 with no Dynamic header.  Per PEP 643, every wheel
# must share these deps; per-wheel fetches are skipped.
_STATIC_DEPS_METADATA = (
    "Metadata-Version: 2.2\n"
    "Name: pkg\n"
    "Version: 1.0\n"
    "Requires-Dist: requests>=2.0\n"
    "Requires-Dist: numpy>=1.0\n"
    "\n"
)

# Same as above but with a micro component on Metadata-Version.
# 2.2.1 is still >= 2.2, so it qualifies for the static fast path.
_STATIC_DEPS_METADATA_MICRO = _STATIC_DEPS_METADATA.replace(
    "Metadata-Version: 2.2", "Metadata-Version: 2.2.1"
)


def _wheel(filename: str) -> WheelFile:
    parts = filename.split("-")
    return WheelFile(
        filename=filename,
        url=f"https://example.com/{filename}",
        version=parts[1] if len(parts) > 1 else "0",
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )


def _linux_311() -> MatrixTuple:
    return MatrixTuple(
        python_version="3.11",
        platform_id="linux_x86_64",
        environment={
            "python_version": "3.11",
            "python_full_version": "3.11.0",
            "implementation_name": "cpython",
            "implementation_version": "3.11.0",
            "os_name": "posix",
            "platform_machine": "x86_64",
            "platform_python_implementation": "CPython",
            "platform_release": "",
            "platform_system": "Linux",
            "platform_version": "",
            "sys_platform": "linux",
        },
        platform_spec=PlatformSpec("linux_x86_64"),
    )


def _windows_311() -> MatrixTuple:
    return MatrixTuple(
        python_version="3.11",
        platform_id="windows_amd64",
        environment={
            "python_version": "3.11",
            "python_full_version": "3.11.0",
            "implementation_name": "cpython",
            "implementation_version": "3.11.0",
            "os_name": "nt",
            "platform_machine": "AMD64",
            "platform_python_implementation": "CPython",
            "platform_release": "",
            "platform_system": "Windows",
            "platform_version": "",
            "sys_platform": "win32",
        },
        platform_spec=PlatformSpec("windows_amd64"),
    )


def _make_coordinator(
    listings: Mapping[str, Sequence[WheelFile | SdistFile]],
    *,
    baseline_metadata: Mapping[str, str] | None = None,
    per_wheel_metadata: Mapping[str, str] | None = None,
    sdist_pyproject: Mapping[str, str] | None = None,
    fetch_failures: set[str] | None = None,
) -> MagicMock:
    """Build a coordinator stub with pre-populated index.

    Thin wrapper around :func:`make_coordinator` so the call sites keep
    the validate-specific names ``baseline_metadata``,
    ``per_wheel_metadata``, ``sdist_pyproject``, and ``fetch_failures``.
    """
    return make_coordinator(
        listings=listings,
        baseline_metadata=baseline_metadata,
        per_wheel_metadata=per_wheel_metadata,
        sdist_pyproject=sdist_pyproject,
        fetch_failures=fetch_failures,
    )


class TestValidateLock:
    """End-to-end validation against in-memory listings + metadata."""

    def test_consistent_metadata_no_issues(self) -> None:
        """When per-wheel metadata matches baseline, all pins are ok."""
        wheel = _wheel("pkg-1.0-cp311-cp311-linux_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            baseline_metadata={"pkg": _BASE_METADATA},
            per_wheel_metadata={wheel.filename: _BASE_METADATA},
        )
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        assert report.pins_checked == 1
        assert report.pins_ok == 1
        assert all(f.status == "ok" for f in report.findings)

    def test_divergent_wheel_metadata_flagged(self) -> None:
        """When a per-wheel metadata differs, ``divergent`` is reported."""
        linux_wheel = _wheel("pkg-1.0-cp311-cp311-linux_x86_64.whl")
        win_wheel = _wheel("pkg-1.0-cp311-cp311-win_amd64.whl")
        coordinator = _make_coordinator(
            {"pkg": [linux_wheel, win_wheel]},
            baseline_metadata={"pkg": _BASE_METADATA},
            per_wheel_metadata={
                linux_wheel.filename: _BASE_METADATA,
                win_wheel.filename: _DIVERGENT_METADATA,
            },
        )
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
                TupleResult(
                    tuple_=_windows_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        assert report.pins_checked == 2
        assert report.pins_ok == 1
        win_finding = next(
            f for f in report.findings if f.tuple_label.startswith("py311-windows")
        )
        assert win_finding.status == "divergent"
        assert "ipywidgets" in win_finding.extra_deps
        assert "numpy" in win_finding.missing_deps

    def test_sdist_only_reported(self) -> None:
        """A version with no wheels reports ``sdist_only``."""
        coordinator = _make_coordinator({"pkg": []})  # listing exists but empty
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        assert report.pins_checked == 1
        assert report.findings[0].status == "sdist_only"

    def test_no_compatible_wheel(self) -> None:
        """When wheels exist but none compatible, ``no_compatible_wheel``."""
        only_win = _wheel("pkg-1.0-cp311-cp311-win_amd64.whl")
        coordinator = _make_coordinator({"pkg": [only_win]})
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        assert report.findings[0].status == "no_compatible_wheel"

    def test_failed_tuple_skipped(self) -> None:
        """Failed tuples contribute no validations."""
        coordinator = _make_coordinator({})
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(tuple_=_linux_311(), success=False, error="nope"),
            ],
        )
        report = validate_lock(result, coordinator)
        assert report.pins_checked == 0

    def test_wheel_without_metadata_url(self) -> None:
        """Wheels with ``has_metadata=False`` are reported as ``no_metadata``."""
        wheel = WheelFile(
            filename="pkg-1.0-py3-none-any.whl",
            url="https://example.com/pkg-1.0.whl",
            version="1.0",
            requires_python=None,
            has_metadata=False,  # the case under test
            upload_time=None,
        )
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            baseline_metadata={"pkg": _BASE_METADATA},
        )
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        assert report.findings[0].status == "no_metadata"

    def test_baseline_missing_treats_as_empty(self) -> None:
        """When no baseline metadata is cached, every dep is ``extra``."""
        wheel = _wheel("pkg-1.0-cp311-cp311-linux_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            per_wheel_metadata={wheel.filename: _BASE_METADATA},
        )
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        # No baseline -> baseline_deps is empty, chosen has 2 deps,
        # extra={requests, numpy}, missing={}.
        finding = report.findings[0]
        assert finding.status == "divergent"
        assert finding.missing_deps == ()
        assert set(finding.extra_deps) == {"requests", "numpy"}


class TestFetchWheelMetadata:
    """The coordinator-driven fetch path is exercised via the stub."""

    def test_fetch_failure_returns_no_metadata(self) -> None:
        """A failed fetch (None stored at sentinel) yields ``no_metadata``."""
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            baseline_metadata={"pkg": _BASE_METADATA},
            fetch_failures={wheel.filename},
        )
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        assert report.findings[0].status == "no_metadata"

    def test_fetch_via_coordinator_populates_cache(self) -> None:
        """A successful coordinator fetch lands metadata at the sentinel key."""
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            baseline_metadata={"pkg": _BASE_METADATA},
            per_wheel_metadata={wheel.filename: _BASE_METADATA},
        )
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        assert report.findings[0].status == "ok"


def _sdist(filename: str) -> SdistFile:
    parts = filename.split("-")
    last = parts[-1]
    if last.endswith(".tar.gz"):
        version = last[: -len(".tar.gz")]
    elif last.endswith(".zip"):
        version = last[: -len(".zip")]
    else:
        version = last
    return SdistFile(
        filename=filename,
        url=f"https://example.com/{filename}",
        version=version,
        requires_python=None,
        upload_time=None,
    )


class TestStaticSdistAuthoritative:
    """When metadata is fully static, per-wheel fetches are skipped."""

    def test_pep643_static_skips_per_wheel_fetch(self) -> None:
        """Metadata 2.2 with no Dynamic header trusts the baseline."""
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            baseline_metadata={"pkg": _STATIC_DEPS_METADATA},
        )
        # Note: no per_wheel_metadata pre-population.  Skipping the
        # fetch is what the test asserts.
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        assert report.findings[0].status == "static_sdist_authoritative"
        # Coordinator's request_wheel_metadata should NOT have been called.
        assert not coordinator.request_wheel_metadata.called

    def test_dynamic_deps_metadata_falls_through(self) -> None:
        """Metadata 2.2 with ``Dynamic: Requires-Dist`` runs full validation."""
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            baseline_metadata={"pkg": _DYNAMIC_DEPS_METADATA},
            per_wheel_metadata={wheel.filename: _DYNAMIC_DEPS_METADATA},
        )
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        # Falls through to per-wheel validation; wheel matches baseline.
        assert report.findings[0].status == "ok"

    def test_old_metadata_version_falls_through(self) -> None:
        """Metadata < 2.2 has no static-deps guarantee."""
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            baseline_metadata={"pkg": _BASE_METADATA},  # 2.1
            per_wheel_metadata={wheel.filename: _BASE_METADATA},
        )
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        # 2.1 metadata cannot use the fast path.
        assert report.findings[0].status == "ok"

    def test_micro_metadata_version_qualifies(self) -> None:
        """A Metadata-Version with a micro part (2.2.1) is still >= 2.2."""
        from nab_python.universal.validate import _baseline_has_static_deps

        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        coordinator = _make_coordinator({"pkg": [wheel]})
        coordinator.index.store_metadata("pkg", "1.0", _STATIC_DEPS_METADATA_MICRO)
        assert _baseline_has_static_deps(coordinator, "pkg", Version("1.0")) is True

    def test_malformed_metadata_falls_through(self) -> None:
        """Garbage metadata never enters the fast path."""
        from nab_python.universal.validate import _baseline_has_static_deps

        # Inject malformed metadata directly into the index, bypassing
        # _make_coordinator's wheel-iteration path.
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        coordinator = _make_coordinator({"pkg": [wheel]})
        coordinator.index.store_metadata("pkg", "1.0", "this isn't valid metadata")
        assert _baseline_has_static_deps(coordinator, "pkg", Version("1.0")) is False

    def test_missing_metadata_version_falls_through(self) -> None:
        """Metadata without a Metadata-Version header doesn't qualify."""
        text = "Name: pkg\nVersion: 1.0\nRequires-Dist: foo\n\n"
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            baseline_metadata={"pkg": text},
            per_wheel_metadata={wheel.filename: text},
        )
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        assert report.findings[0].status == "ok"

    def test_unparseable_metadata_version_falls_through(self) -> None:
        """A non-numeric Metadata-Version is treated as old."""
        text = (
            "Metadata-Version: garbage\nName: pkg\nVersion: 1.0\nRequires-Dist: foo\n\n"
        )
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            baseline_metadata={"pkg": text},
            per_wheel_metadata={wheel.filename: text},
        )
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        assert report.findings[0].status == "ok"


class TestPyprojectStaticDepsFastPath:
    """PEP 621 ``pyproject.toml`` route into ``static_sdist_authoritative``.

    When METADATA marks ``Requires-Dist`` Dynamic but the sdist's
    pyproject.toml has static ``[project].dependencies``, the deps
    are still authoritative for every wheel built from this sdist.
    """

    _PYPROJECT_STATIC = (
        '[project]\nname = "pkg"\nversion = "1.0"\ndependencies = ["requests"]\n'
    )
    _PYPROJECT_DYNAMIC_DEPS = (
        '[project]\nname = "pkg"\nversion = "1.0"\ndynamic = ["dependencies"]\n'
    )
    _PYPROJECT_DYNAMIC_OPTIONAL = (
        '[project]\nname = "pkg"\nversion = "1.0"\n'
        "dependencies = []\n"
        'dynamic = ["optional-dependencies"]\n'
    )

    def test_static_pyproject_overrides_dynamic_metadata(self) -> None:
        """Static pyproject lets us trust the baseline despite Dynamic."""
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            baseline_metadata={"pkg": _DYNAMIC_DEPS_METADATA},
            sdist_pyproject={"pkg": self._PYPROJECT_STATIC},
        )
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        assert report.findings[0].status == "static_sdist_authoritative"
        assert not coordinator.request_wheel_metadata.called

    def test_dynamic_dependencies_in_pyproject_falls_through(self) -> None:
        """When pyproject lists dependencies as dynamic, no fast path."""
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            baseline_metadata={"pkg": _DYNAMIC_DEPS_METADATA},
            per_wheel_metadata={wheel.filename: _DYNAMIC_DEPS_METADATA},
            sdist_pyproject={"pkg": self._PYPROJECT_DYNAMIC_DEPS},
        )
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        # Falls through to per-wheel; deps match.
        assert report.findings[0].status == "ok"

    def test_dynamic_optional_deps_falls_through(self) -> None:
        """``optional-dependencies`` in dynamic also blocks the fast path."""
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            baseline_metadata={"pkg": _DYNAMIC_DEPS_METADATA},
            per_wheel_metadata={wheel.filename: _DYNAMIC_DEPS_METADATA},
            sdist_pyproject={"pkg": self._PYPROJECT_DYNAMIC_OPTIONAL},
        )
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        assert report.findings[0].status == "ok"

    def test_pyproject_without_project_table_falls_through(self) -> None:
        """A pyproject.toml without ``[project]`` doesn't qualify."""
        from nab_python.universal.validate import _pyproject_is_pep621_static

        coordinator = _make_coordinator({"pkg": []})
        coordinator.index.store_sdist_pyproject(
            "pkg",
            "1.0",
            '[build-system]\nrequires = ["setuptools"]\n',
        )
        assert _pyproject_is_pep621_static(coordinator, "pkg", Version("1.0")) is False

    def test_malformed_pyproject_falls_through(self) -> None:
        """Garbage TOML doesn't qualify."""
        from nab_python.universal.validate import _pyproject_is_pep621_static

        coordinator = _make_coordinator({"pkg": []})
        coordinator.index.store_sdist_pyproject(
            "pkg", "1.0", "this isn't valid TOML !!!"
        )
        assert _pyproject_is_pep621_static(coordinator, "pkg", Version("1.0")) is False

    def test_missing_pyproject_falls_through(self) -> None:
        """No pyproject.toml in the index returns False."""
        from nab_python.universal.validate import _pyproject_is_pep621_static

        coordinator = _make_coordinator({"pkg": []})
        assert _pyproject_is_pep621_static(coordinator, "pkg", Version("1.0")) is False

    def test_project_with_no_dependencies_qualifies(self) -> None:
        """A ``[project]`` table without ``dependencies`` still qualifies.

        Per PEP 621, a missing ``dependencies`` field with no
        ``dynamic`` flag means "no runtime deps".  That is itself
        an authoritative static value.
        """
        from nab_python.universal.validate import _pyproject_is_pep621_static

        coordinator = _make_coordinator({"pkg": []})
        coordinator.index.store_sdist_pyproject(
            "pkg",
            "1.0",
            '[project]\nname = "pkg"\nversion = "1.0"\n',
        )
        assert _pyproject_is_pep621_static(coordinator, "pkg", Version("1.0")) is True

    def test_non_string_dynamic_entries_ignored(self) -> None:
        """Non-string entries in ``[project].dynamic`` are ignored."""
        from nab_python.universal.validate import _pyproject_is_pep621_static

        coordinator = _make_coordinator({"pkg": []})
        coordinator.index.store_sdist_pyproject(
            "pkg",
            "1.0",
            '[project]\nname = "pkg"\nversion = "1.0"\n'
            'dynamic = [42, true, "version"]\n',
        )
        # 42 and true are dropped; "version" doesn't affect deps.
        assert _pyproject_is_pep621_static(coordinator, "pkg", Version("1.0")) is True


class TestNoCompatibleWheelWithSdist:
    """When wheels exist but none compatible, sdist availability matters."""

    def test_sdist_present_yields_buildable_status(self) -> None:
        """``no_compatible_wheel_with_sdist`` when sdist is in the listing."""
        only_win = _wheel("pkg-1.0-cp311-cp311-win_amd64.whl")
        sdist = _sdist("pkg-1.0.tar.gz")
        coordinator = _make_coordinator({"pkg": [only_win, sdist]})
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        assert report.findings[0].status == "no_compatible_wheel_with_sdist"

    def test_sdist_absent_yields_hard_error(self) -> None:
        """``no_compatible_wheel`` when no sdist is available."""
        only_win = _wheel("pkg-1.0-cp311-cp311-win_amd64.whl")
        coordinator = _make_coordinator({"pkg": [only_win]})
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        assert report.findings[0].status == "no_compatible_wheel"


class TestFatalFindings:
    """``fatal_findings`` enforces the lock policy."""

    def test_no_compatible_wheel_always_fatal(self) -> None:
        """``no_compatible_wheel`` fails regardless of build_allowed."""
        f = PinValidation(
            tuple_label="x",
            package="p",
            version="1.0",
            status="no_compatible_wheel",
        )
        report = ValidationReport(pins_checked=1, findings=[f])
        assert report.fatal_findings(build_allowed=False) == [f]
        assert report.fatal_findings(build_allowed=True) == [f]

    def test_no_metadata_always_fatal(self) -> None:
        """``no_metadata`` fails regardless of build_allowed."""
        f = PinValidation(
            tuple_label="x",
            package="p",
            version="1.0",
            status="no_metadata",
        )
        report = ValidationReport(pins_checked=1, findings=[f])
        assert report.fatal_findings(build_allowed=True) == [f]

    def test_sdist_only_fatal_under_no_build(self) -> None:
        """``sdist_only`` is fatal when builds are not allowed."""
        f = PinValidation(
            tuple_label="x",
            package="p",
            version="1.0",
            status="sdist_only",
        )
        report = ValidationReport(pins_checked=1, findings=[f])
        assert report.fatal_findings(build_allowed=False) == [f]
        assert report.fatal_findings(build_allowed=True) == []

    def test_no_compatible_wheel_with_sdist_fatal_under_no_build(self) -> None:
        """``no_compatible_wheel_with_sdist`` requires build."""
        f = PinValidation(
            tuple_label="x",
            package="p",
            version="1.0",
            status="no_compatible_wheel_with_sdist",
        )
        report = ValidationReport(pins_checked=1, findings=[f])
        assert report.fatal_findings(build_allowed=False) == [f]
        assert report.fatal_findings(build_allowed=True) == []

    def test_divergent_not_fatal(self) -> None:
        """``divergent`` is a warning, not fatal."""
        f = PinValidation(
            tuple_label="x",
            package="p",
            version="1.0",
            status="divergent",
        )
        report = ValidationReport(pins_checked=1, findings=[f])
        assert report.fatal_findings(build_allowed=False) == []

    def test_static_authoritative_not_fatal(self) -> None:
        """``static_sdist_authoritative`` is sound, not fatal."""
        f = PinValidation(
            tuple_label="x",
            package="p",
            version="1.0",
            status="static_sdist_authoritative",
        )
        report = ValidationReport(pins_checked=1, findings=[f])
        assert report.fatal_findings(build_allowed=False) == []


_BASE_EXTRA_METADATA = (
    "Metadata-Version: 2.1\n"
    "Name: pkg\n"
    "Version: 1.0\n"
    "Provides-Extra: redis\n"
    "Provides-Extra: postgres\n"
    "Requires-Dist: requests>=2.0\n"
    'Requires-Dist: redis>=5.0; extra == "redis"\n'
    'Requires-Dist: psycopg2>=2.9; extra == "postgres"\n'
    "\n"
)

# Same Provides-Extra set, but the redis extra ships an extra dep.
_DIVERGENT_EXTRA_METADATA = (
    "Metadata-Version: 2.1\n"
    "Name: pkg\n"
    "Version: 1.0\n"
    "Provides-Extra: redis\n"
    "Provides-Extra: postgres\n"
    "Requires-Dist: requests>=2.0\n"
    'Requires-Dist: redis>=5.0; extra == "redis"\n'
    'Requires-Dist: hiredis>=2.0; extra == "redis"\n'
    'Requires-Dist: psycopg2>=2.9; extra == "postgres"\n'
    "\n"
)


class TestPerExtraDivergence:
    """Hole 11 plug: per-extra dep divergence between baseline and chosen wheel."""

    def test_evaluate_metadata_deps_by_extra_groups_correctly(self) -> None:
        """``_evaluate_metadata_deps_by_extra`` returns base + per-extra buckets."""
        from nab_python.universal.validate import _evaluate_metadata_deps_by_extra

        env = _linux_311().environment
        out = _evaluate_metadata_deps_by_extra(_BASE_EXTRA_METADATA, env)
        assert out[None] == {"requests"}
        assert out["redis"] == {"redis"}
        assert out["postgres"] == {"psycopg2"}

    def test_extras_divergent_status_when_only_extra_differs(self) -> None:
        """Base deps match but the redis extra differs -> ``divergent_in_extra``."""
        wheel = _wheel("pkg-1.0-cp311-cp311-linux_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            baseline_metadata={"pkg": _BASE_EXTRA_METADATA},
            per_wheel_metadata={wheel.filename: _DIVERGENT_EXTRA_METADATA},
        )
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        finding = report.findings[0]
        assert finding.status == "divergent_in_extra"
        assert len(finding.extras_divergent) == 1
        diff = finding.extras_divergent[0]
        assert diff.extra == "redis"
        assert diff.extra_deps == ("hiredis",)
        assert diff.missing_deps == ()

    def test_base_diff_keeps_divergent_status_with_extras_attached(self) -> None:
        """When base deps differ, status stays ``divergent`` plus extras detail."""
        # Baseline lists redis only; chosen drops redis from base AND
        # diverges in the redis extra.  Base diff: missing redis from chosen.
        baseline = (
            "Metadata-Version: 2.1\n"
            "Name: pkg\n"
            "Version: 1.0\n"
            "Provides-Extra: redis\n"
            "Requires-Dist: requests>=2.0\n"
            "Requires-Dist: redis>=5.0\n"
            'Requires-Dist: redis>=5.0; extra == "redis"\n'
            "\n"
        )
        chosen = (
            "Metadata-Version: 2.1\n"
            "Name: pkg\n"
            "Version: 1.0\n"
            "Provides-Extra: redis\n"
            "Requires-Dist: requests>=2.0\n"
            'Requires-Dist: redis>=5.0; extra == "redis"\n'
            'Requires-Dist: hiredis>=2.0; extra == "redis"\n'
            "\n"
        )
        wheel = _wheel("pkg-1.0-cp311-cp311-linux_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            baseline_metadata={"pkg": baseline},
            per_wheel_metadata={wheel.filename: chosen},
        )
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        finding = report.findings[0]
        assert finding.status == "divergent"
        assert "redis" in finding.missing_deps
        assert finding.extras_divergent  # also reports extra divergence

    def test_consistent_extras_dont_emit_finding(self) -> None:
        """Identical metadata across wheels yields ``ok``."""
        wheel = _wheel("pkg-1.0-cp311-cp311-linux_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            baseline_metadata={"pkg": _BASE_EXTRA_METADATA},
            per_wheel_metadata={wheel.filename: _BASE_EXTRA_METADATA},
        )
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        finding = report.findings[0]
        assert finding.status == "ok"
        assert finding.extras_divergent == ()

    def test_extra_present_only_in_chosen_wheel(self) -> None:
        """A new extra in the chosen wheel reports its deps as ``extra_deps``."""
        baseline = (
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
            "Provides-Extra: redis\n"
            "Requires-Dist: requests>=2.0\n"
            'Requires-Dist: redis>=5.0; extra == "redis"\n\n'
        )
        chosen = (
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
            "Provides-Extra: redis\n"
            "Provides-Extra: pgsql\n"
            "Requires-Dist: requests>=2.0\n"
            'Requires-Dist: redis>=5.0; extra == "redis"\n'
            'Requires-Dist: psycopg2>=2.9; extra == "pgsql"\n\n'
        )
        wheel = _wheel("pkg-1.0-cp311-cp311-linux_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            baseline_metadata={"pkg": baseline},
            per_wheel_metadata={wheel.filename: chosen},
        )
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = validate_lock(result, coordinator)
        finding = report.findings[0]
        assert finding.status == "divergent_in_extra"
        diff = next(d for d in finding.extras_divergent if d.extra == "pgsql")
        assert diff.extra_deps == ("psycopg2",)
