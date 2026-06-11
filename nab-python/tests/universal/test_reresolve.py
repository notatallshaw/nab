"""Tests for the divergence-driven re-resolve pass.

Heavy mocking: ``reresolve_divergent_tuples`` calls
``resolve_with_coordinator`` to re-run the resolver with overridden
metadata.  The tests stub the inner resolve so we can drive the diff
output directly.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from nab_index.client import WheelFile
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python._vendor.packaging.version import Version
from nab_python.provider import BuildPolicy, DistPolicy
from nab_python.universal import reresolve as reresolve_module
from nab_python.universal.matrix import MatrixTuple
from nab_python.universal.reresolve import (
    _diff_pins,
    _resolve_one_tuple_with_overrides,
    reresolve_divergent_tuples,
)
from nab_python.universal.resolve import TupleResult, UniversalResult
from nab_python.universal.validate import PinValidation, ValidationReport
from nab_python.universal.wheel_selection import PlatformSpec


def _wheel(filename: str) -> WheelFile:
    parts = filename.split("-")
    version = parts[1] if len(parts) > 1 else "0"
    return WheelFile(
        filename=filename,
        url=f"https://example.com/{filename}",
        version=version,
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )


def _make_coordinator(
    listings: dict[str, list[WheelFile]],
    *,
    per_wheel_metadata: dict[str, str] | None = None,
) -> MagicMock:
    """Stub coordinator with index pre-populated for re-resolve tests."""
    return make_coordinator(
        listings=listings,
        per_wheel_metadata=per_wheel_metadata,
    )


def _linux_311(full_version: str = "3.11.0") -> MatrixTuple:
    return MatrixTuple(
        python_version="3.11",
        platform_id="linux_x86_64",
        environment={
            "python_version": "3.11",
            "python_full_version": full_version,
            "implementation_name": "cpython",
            "implementation_version": full_version,
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


class TestDiffPins:
    """``_diff_pins`` computes added / removed / version_changed."""

    def test_no_change(self) -> None:
        """Identical pin sets yield an empty diff."""
        diff = _diff_pins(
            "label",
            new_pins={"a": "1.0", "b": "2.0"},
            original_pins={"a": "1.0", "b": "2.0"},
        )
        assert not diff.added
        assert not diff.removed
        assert not diff.version_changed

    def test_added_packages(self) -> None:
        """Packages absent from original become ``added`` entries."""
        diff = _diff_pins(
            "label",
            new_pins={"a": "1.0", "newpkg": "2.0"},
            original_pins={"a": "1.0"},
        )
        assert diff.added == {"newpkg": "2.0"}

    def test_removed_packages(self) -> None:
        """Packages absent from new pins become ``removed`` entries."""
        diff = _diff_pins(
            "label",
            new_pins={"a": "1.0"},
            original_pins={"a": "1.0", "gone": "2.0"},
        )
        assert diff.removed == {"gone": "2.0"}

    def test_version_changed(self) -> None:
        """Same package, different version is a version change."""
        diff = _diff_pins(
            "label",
            new_pins={"a": "2.0"},
            original_pins={"a": "1.0"},
        )
        assert diff.version_changed == {"a": ("1.0", "2.0")}

    def test_no_original_pins_makes_everything_added(self) -> None:
        """When original is None, everything in new_pins is added."""
        diff = _diff_pins("label", new_pins={"a": "1.0"}, original_pins=None)
        assert diff.added == {"a": "1.0"}


class TestReresolveDivergentTuples:
    """End-to-end through ``reresolve_divergent_tuples``."""

    def test_no_divergent_no_reresolve(self) -> None:
        """Tuples with no divergent findings are skipped."""
        coordinator = _make_coordinator({})
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
        # Build a report with only ``ok`` findings.
        report = ValidationReport(
            findings=[
                PinValidation(
                    tuple_label="py311-linux_x86_64",
                    package="pkg",
                    version="1.0",
                    status="ok",
                ),
            ],
        )
        diffs = reresolve_divergent_tuples(coordinator, [], result, report)
        assert diffs == {}

    def test_failed_tuple_skipped(self) -> None:
        """A failed original tuple is not re-resolved."""
        coordinator = _make_coordinator({})
        result = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(tuple_=_linux_311(), success=False, error="x"),
            ],
        )
        report = ValidationReport()
        diffs = reresolve_divergent_tuples(coordinator, [], result, report)
        assert diffs == {}

    def test_divergent_with_metadata_reresolves(self) -> None:
        """A divergent finding with cached metadata triggers re-resolve.

        Patches ``resolve_with_coordinator`` so the test does not run
        the actual PubGrub algorithm; we just verify the call happens
        and the diff is computed correctly.
        """
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            per_wheel_metadata={
                wheel.filename: "Name: pkg\nVersion: 1.0\nRequires-Dist: foo\n\n"
            },
        )
        original = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={
                        "pkg": Version("1.0"),
                        "old-only": Version("3.0"),
                    },
                ),
            ],
        )
        report = ValidationReport(
            findings=[
                PinValidation(
                    tuple_label="py311-linux_x86_64",
                    package="pkg",
                    version="1.0",
                    status="divergent",
                    chosen_wheel=wheel.filename,
                ),
            ],
        )
        # Stub the inner helper so we don't run the full resolver.
        new_pins = {"pkg": "1.0", "newdep": "4.0"}
        with patch.object(
            reresolve_module,
            "_resolve_one_tuple_with_overrides",
            return_value=new_pins,
        ):
            diffs = reresolve_divergent_tuples(coordinator, ["pkg"], original, report)
        assert "py311-linux_x86_64" in diffs
        diff = diffs["py311-linux_x86_64"]
        assert diff.added == {"newdep": "4.0"}
        assert diff.removed == {"old-only": "3.0"}

    def test_failed_reresolve_yields_all_removed(self) -> None:
        """When the inner re-resolve fails, ``new_pins`` is empty.

        The diff records every original pin as removed; the caller
        can detect that and choose to keep the original lock.
        """
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        coordinator = _make_coordinator(
            {"pkg": [wheel]},
            per_wheel_metadata={
                wheel.filename: "Name: pkg\nVersion: 1.0\nRequires-Dist: foo\n\n"
            },
        )
        original = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0")},
                ),
            ],
        )
        report = ValidationReport(
            findings=[
                PinValidation(
                    tuple_label="py311-linux_x86_64",
                    package="pkg",
                    version="1.0",
                    status="divergent",
                    chosen_wheel=wheel.filename,
                ),
            ],
        )
        with patch.object(
            reresolve_module,
            "_resolve_one_tuple_with_overrides",
            return_value={},
        ):
            diffs = reresolve_divergent_tuples(coordinator, ["pkg"], original, report)
        diff = diffs["py311-linux_x86_64"]
        assert diff.removed == {"pkg": "1.0"}


class TestCollectWheelMetadataOverrides:
    """``_collect_wheel_metadata_overrides`` reads the sentinel cache."""

    def test_skips_when_no_metadata_cached(self) -> None:
        """A finding whose wheel metadata isn't in the index is skipped."""
        from nab_python.universal.reresolve import _collect_wheel_metadata_overrides

        coordinator = _make_coordinator(
            {"pkg": [_wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")]},
        )  # no per_wheel_metadata; index will return None
        findings = [
            PinValidation(
                tuple_label="x",
                package="pkg",
                version="1.0",
                status="divergent",
                chosen_wheel="pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl",
            ),
        ]
        out = _collect_wheel_metadata_overrides(coordinator, findings)
        assert out == {}


class TestOverrideMetadata:
    """``_override_metadata`` snapshots, overrides, and restores."""

    def test_snapshot_and_restore(self) -> None:
        """Pre-existing metadata is restored on context exit."""
        from nab_python.universal.reresolve import _override_metadata

        coordinator = _make_coordinator({})
        coordinator.index.store_metadata("pkg", "1.0", "ORIGINAL")
        with _override_metadata(coordinator, {("pkg", "1.0"): "OVERRIDE"}):
            assert coordinator.index.get_metadata("pkg", "1.0") == "OVERRIDE"
        # Restored.
        assert coordinator.index.get_metadata("pkg", "1.0") == "ORIGINAL"

    def test_parsed_metadata_evicted_then_restored(self) -> None:
        """A cached parsed view is evicted inside and restored on exit."""
        from nab_python.universal.reresolve import _override_metadata

        coordinator = _make_coordinator({})
        coordinator.index.store_metadata("pkg", "1.0", "ORIGINAL")
        sentinel = object()
        coordinator.index.store_parsed_metadata("pkg", "1.0", sentinel)
        with _override_metadata(coordinator, {("pkg", "1.0"): "OVERRIDE"}):
            assert coordinator.index.get_parsed_metadata("pkg", "1.0") is None
        assert coordinator.index.get_parsed_metadata("pkg", "1.0") is sentinel

    def test_no_prior_parsed_metadata_leaves_cache_empty(self) -> None:
        """Override without a prior parsed entry leaves the cache empty on exit."""
        from nab_python.universal.reresolve import _override_metadata

        coordinator = _make_coordinator({})
        coordinator.index.store_metadata("pkg", "1.0", "ORIGINAL")
        with _override_metadata(coordinator, {("pkg", "1.0"): "OVERRIDE"}):
            coordinator.index.store_parsed_metadata("pkg", "1.0", object())
        assert coordinator.index.get_parsed_metadata("pkg", "1.0") is None

    def test_restore_preserves_sdist_provenance(self) -> None:
        """An sdist-origin baseline is still sdist-origin after restore."""
        from nab_python.universal.reresolve import _override_metadata

        coordinator = _make_coordinator({})
        coordinator.index.store_sdist_metadata("pkg", "1.0", "PKG-INFO")
        with _override_metadata(coordinator, {("pkg", "1.0"): "OVERRIDE"}):
            assert not coordinator.index.metadata_from_sdist("pkg", "1.0")
        assert coordinator.index.get_metadata("pkg", "1.0") == "PKG-INFO"
        assert coordinator.index.metadata_from_sdist("pkg", "1.0")


class TestResolveOneTupleWithOverrides:
    """``_resolve_one_tuple_with_overrides`` builds a one-tuple matrix."""

    def test_failed_inner_resolve_returns_empty(self) -> None:
        """When the inner resolve fails, we return empty pins."""
        from nab_python.provider import BuildPolicy, DistPolicy
        from nab_python.universal.reresolve import _resolve_one_tuple_with_overrides

        coordinator = _make_coordinator({})
        failed = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(tuple_=_linux_311(), success=False, error="x"),
            ],
        )
        with patch.object(
            reresolve_module,
            "resolve_with_coordinator",
            return_value=failed,
        ):
            pins = _resolve_one_tuple_with_overrides(
                coordinator,
                _linux_311(),
                ["pkg"],
                {("pkg", "1.0"): "Name: pkg\nVersion: 1.0\n\n"},
                constraints=None,
                uploaded_prior_to=None,
                dist_policy=DistPolicy.WHEEL_OR_SDIST,
                build_policy=BuildPolicy.NEVER,
                resolution_strategy="highest",
            )
        assert pins == {}

    def test_successful_inner_resolve_returns_pins(self) -> None:
        """Successful re-resolve returns the new pin dict."""
        from nab_python.provider import BuildPolicy, DistPolicy
        from nab_python.universal.reresolve import _resolve_one_tuple_with_overrides

        coordinator = _make_coordinator({})
        ok = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[
                TupleResult(
                    tuple_=_linux_311(),
                    success=True,
                    pins={"pkg": Version("1.0"), "dep": Version("2.0")},
                ),
            ],
        )
        with patch.object(
            reresolve_module,
            "resolve_with_coordinator",
            return_value=ok,
        ):
            pins = _resolve_one_tuple_with_overrides(
                coordinator,
                _linux_311(),
                ["pkg"],
                {("pkg", "1.0"): "Name: pkg\nVersion: 1.0\n\n"},
                constraints=None,
                uploaded_prior_to=None,
                dist_policy=DistPolicy.WHEEL_OR_SDIST,
                build_policy=BuildPolicy.NEVER,
                resolution_strategy="highest",
            )
        assert pins == {"pkg": "1.0", "dep": "2.0"}

    def test_one_tuple_matrix_keeps_python_patch_release(self) -> None:
        """The re-resolve matrix carries the original tuple's patch release.

        A tuple resolved with ``python_patches`` must re-resolve under
        the same ``python_full_version``, otherwise markers gated on
        the patch release flip between the two passes and pollute the
        diff.
        """
        tup = _linux_311(full_version="3.11.9")
        coordinator = _make_coordinator({})
        ok = UniversalResult(
            matrix=MagicMock(),
            tuple_results=[TupleResult(tuple_=tup, success=True, pins={})],
        )
        with patch.object(
            reresolve_module,
            "resolve_with_coordinator",
            return_value=ok,
        ) as inner:
            _resolve_one_tuple_with_overrides(
                coordinator,
                tup,
                ["pkg"],
                {("pkg", "1.0"): "Name: pkg\nVersion: 1.0\n\n"},
                constraints=None,
                uploaded_prior_to=None,
                dist_policy=DistPolicy.WHEEL_OR_SDIST,
                build_policy=BuildPolicy.NEVER,
                resolution_strategy="highest",
            )
        matrix = inner.call_args.args[1]
        env = matrix.expand()[0].environment
        assert env["python_full_version"] == "3.11.9"
        assert env["implementation_version"] == "3.11.9"


_DIVERGENT_BASELINE_METADATA = (
    "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\nRequires-Dist: oldlib\n\n"
)
_DIVERGENT_WHEEL_METADATA = (
    "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\nRequires-Dist: newlib\n\n"
)
