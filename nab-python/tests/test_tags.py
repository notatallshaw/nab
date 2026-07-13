"""Tests for the wheel-selection logic backed by ``packaging.tags``.

These tests pin the spec-compliant tag selector's behavior on the
common cases that real resolvers encounter:

- manylinux / musllinux floor enforcement (PEP 600 / PEP 656)
- macOS arch + version-floor compatibility
- Windows arch matching
- ``py3-none-any`` fallback ordering
- Compressed-tag-set wheels (``cp310.cp311``)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nab_index.client import WheelFile
from nab_python.tags import (
    _PLATFORM_ARCH,
    _PLATFORM_KIND,
    PlatformSpec,
    _platform_tags_for_spec,
    select_wheel,
    tags_for_target,
)


def _compatible(
    wheel: WheelFile,
    *,
    python_version: str,
    spec: PlatformSpec,
    implementation: str = "cpython",
) -> bool:
    """True iff the target would install ``wheel``, given only that wheel."""
    chosen = select_wheel(
        [wheel],
        python_version=python_version,
        spec=spec,
        implementation=implementation,
    )
    return chosen is not None


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


class TestPlatformSpec:
    """``PlatformSpec`` exposes per-platform tag floors."""

    def test_default_linux_arch(self) -> None:
        """``linux_x86_64`` carries the right architecture name."""
        spec = PlatformSpec("linux_x86_64")
        assert spec.arch == "x86_64"

    def test_macos_arch(self) -> None:
        """``macos_arm64`` arch is ``arm64``."""
        spec = PlatformSpec("macos_arm64")
        assert spec.arch == "arm64"

    def test_label_suffix_distinguishes_space_from_underscore(self) -> None:
        """Whitespace and ``_`` in ``platform_release`` encode differently.

        Both are legal release characters; if they folded to the same
        suffix the two specs' matrix tuples would share a label and
        their per-tuple pins would silently merge.
        """
        with_space = PlatformSpec("linux_x86_64", platform_release="a a")
        with_underscore = PlatformSpec("linux_x86_64", platform_release="a_a")
        assert with_space.label_suffix() != with_underscore.label_suffix()

    def test_label_suffix_release_cannot_forge_version_field(self) -> None:
        """A ``-`` in ``platform_release`` cannot fake a ``ver`` field."""
        release_only = PlatformSpec("linux_x86_64", platform_release="r-ver1")
        release_and_version = PlatformSpec(
            "linux_x86_64", platform_release="r", platform_version="1"
        )
        assert release_only.label_suffix() != release_and_version.label_suffix()

    def test_label_suffix_escapes_kernel_release(self) -> None:
        """Pins the escaped suffix shape for a realistic kernel release."""
        spec = PlatformSpec("linux_x86_64", platform_release="5.15.0-generic")
        assert spec.label_suffix() == "-glibc2.17-musl1.2-rel5.15.0_2d_generic"


class TestTagsForTarget:
    """``tags_for_target`` produces the full PEP 425 tag set."""

    def test_linux_includes_manylinux_at_or_below_floor(self) -> None:
        """A linux_x86_64 spec with floor 2.17 admits manylinux_2_17 and below."""
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "cp311-cp311-manylinux_2_17_x86_64" in tag_strs
        assert "cp311-cp311-manylinux_2_5_x86_64" in tag_strs

    def test_linux_excludes_manylinux_above_floor(self) -> None:
        """manylinux_2_28 is NOT in the set when floor is 2.17."""
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "cp311-cp311-manylinux_2_28_x86_64" not in tag_strs

    def test_linux_includes_legacy_aliases(self) -> None:
        """manylinux1, manylinux2010, manylinux2014 aliases are included."""
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "cp311-cp311-manylinux1_x86_64" in tag_strs
        assert "cp311-cp311-manylinux2010_x86_64" in tag_strs
        assert "cp311-cp311-manylinux2014_x86_64" in tag_strs

    def test_linux_excludes_aliases_above_floor(self) -> None:
        """manylinux2014 is excluded when floor is below 2.17."""
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 12))
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "cp311-cp311-manylinux2014_x86_64" not in tag_strs
        assert "cp311-cp311-manylinux2010_x86_64" in tag_strs

    def test_aarch64_manylinux_floor_stops_at_2_17(self) -> None:
        """aarch64 does not descend below glibc 2.17 (PEP 599)."""
        spec = PlatformSpec("linux_aarch64", manylinux_floor=(2, 17))
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "cp311-cp311-manylinux_2_17_aarch64" in tag_strs
        assert "cp311-cp311-manylinux2014_aarch64" in tag_strs
        assert "cp311-cp311-manylinux_2_16_aarch64" not in tag_strs
        assert "cp311-cp311-manylinux_2_5_aarch64" not in tag_strs
        assert "cp311-cp311-manylinux2010_aarch64" not in tag_strs
        assert "cp311-cp311-manylinux1_aarch64" not in tag_strs

    def test_aarch64_rejects_legacy_alias_wheels(self) -> None:
        """A manylinux1/manylinux2010 aarch64 wheel is not installable."""
        spec = PlatformSpec("linux_aarch64")
        for alias in ("manylinux1", "manylinux2010"):
            wheel = _wheel(f"foo-1.0-cp311-cp311-{alias}_aarch64.whl")
            assert not _compatible(wheel, python_version="3.11", spec=spec)

    def test_x86_64_keeps_legacy_alias_floor(self) -> None:
        """x86_64 still descends to manylinux1 (glibc 2.5)."""
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "cp311-cp311-manylinux1_x86_64" in tag_strs
        assert "cp311-cp311-manylinux_2_5_x86_64" in tag_strs

    def test_non_glibc2_floor_descends_to_x_0(self) -> None:
        """A glibc 3.x floor descends to 3.0 on any arch (the 2.17 cap is glibc 2.x only)."""
        spec = PlatformSpec("linux_aarch64", manylinux_floor=(3, 1))
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "cp311-cp311-manylinux_3_1_aarch64" in tag_strs
        assert "cp311-cp311-manylinux_3_0_aarch64" in tag_strs

    def test_linux_includes_musllinux_at_floor(self) -> None:
        """Musllinux at-or-below the floor is admitted."""
        spec = PlatformSpec("linux_x86_64", musllinux_floor=(1, 2))
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "cp311-cp311-musllinux_1_2_x86_64" in tag_strs
        assert "cp311-cp311-musllinux_1_0_x86_64" in tag_strs

    def test_macos_arm64_default_accepts_modern_wheels(self) -> None:
        """The default ``macos_arm64`` admits ``macosx_11_0`` and ``macosx_12_0``."""
        spec = PlatformSpec("macos_arm64")
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        # mac_platforms yields versions <= the declared one
        assert "cp311-cp311-macosx_11_0_arm64" in tag_strs
        assert "cp311-cp311-macosx_12_0_arm64" in tag_strs
        # Still a ceiling: a newer-than-default macOS wheel is excluded.
        assert "cp311-cp311-macosx_13_0_arm64" not in tag_strs

    def test_macos_x86_64_uses_default_floor(self) -> None:
        """``macos_x86_64`` defaults to macOS 10.13 (x86_64-era)."""
        spec = PlatformSpec("macos_x86_64")
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "cp311-cp311-macosx_10_13_x86_64" in tag_strs

    def test_macos_explicit_min(self) -> None:
        """An explicit ``macos_min`` overrides the default."""
        spec = PlatformSpec("macos_arm64", macos_min=(14, 0))
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "cp311-cp311-macosx_14_0_arm64" in tag_strs

    def test_windows_amd64(self) -> None:
        """Windows amd64 generates ``win_amd64`` tags."""
        spec = PlatformSpec("windows_amd64")
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "cp311-cp311-win_amd64" in tag_strs

    def test_includes_universal_tags(self) -> None:
        """``py3-none-any`` and ``cp311-none-any`` are always in the set."""
        spec = PlatformSpec("linux_x86_64")
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "py3-none-any" in tag_strs
        assert "cp311-none-any" in tag_strs


class TestWheelCompatibility:
    """A wheel is a candidate iff its tags meet the target's tag set."""

    def test_accepts_at_floor_manylinux(self) -> None:
        """A manylinux_2_17 wheel matches a 2.17-floor linux spec."""
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        assert _compatible(wheel, python_version="3.11", spec=spec)

    def test_rejects_above_floor_manylinux(self) -> None:
        """A manylinux_2_28 wheel does not match a 2.17-floor linux spec."""
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_28_x86_64.whl")
        assert not _compatible(wheel, python_version="3.11", spec=spec)

    def test_accepts_universal_wheel(self) -> None:
        """``py3-none-any`` is accepted by every platform."""
        wheel = _wheel("pkg-1.0-py3-none-any.whl")
        for platform_id in (
            "linux_x86_64",
            "macos_arm64",
            "windows_amd64",
        ):
            spec = PlatformSpec(platform_id)
            assert _compatible(wheel, python_version="3.11", spec=spec)

    def test_rejects_wrong_arch(self) -> None:
        """An aarch64 wheel does not match a x86_64 spec."""
        spec = PlatformSpec("linux_x86_64")
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_aarch64.whl")
        assert not _compatible(wheel, python_version="3.11", spec=spec)

    def test_rejects_wrong_python_minor(self) -> None:
        """A cp311 wheel does not match a 3.12 tuple."""
        spec = PlatformSpec("linux_x86_64")
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        assert not _compatible(wheel, python_version="3.12", spec=spec)

    def test_compressed_tag_set(self) -> None:
        """A wheel with cp310.cp311 matches both 3.10 and 3.11."""
        spec = PlatformSpec("linux_x86_64")
        wheel = _wheel("pkg-1.0-cp310.cp311-cp310.cp311-manylinux_2_17_x86_64.whl")
        assert _compatible(wheel, python_version="3.10", spec=spec)
        assert _compatible(wheel, python_version="3.11", spec=spec)

    def test_abi3_forward_compat(self) -> None:
        """A cp310-abi3 wheel works on 3.10, 3.11, etc."""
        spec = PlatformSpec("linux_x86_64")
        wheel = _wheel("pkg-1.0-cp310-abi3-manylinux_2_17_x86_64.whl")
        assert _compatible(wheel, python_version="3.10", spec=spec)
        assert _compatible(wheel, python_version="3.13", spec=spec)

    def test_default_macos_arm64_accepts_modern_arm64_wheel(self) -> None:
        """``macosx_11_0`` and ``macosx_12_0`` wheels match the default spec."""
        spec = PlatformSpec("macos_arm64")
        for tag in ("macosx_11_0_arm64", "macosx_12_0_arm64"):
            wheel = _wheel(f"pkg-1.0-cp311-cp311-{tag}.whl")
            assert _compatible(wheel, python_version="3.11", spec=spec)

    def test_garbage_filename(self) -> None:
        """A non-wheel filename is rejected."""
        spec = PlatformSpec("linux_x86_64")
        wheel = WheelFile(
            filename="garbage.whl",
            url="",
            version="0",
            requires_python=None,
            has_metadata=False,
            upload_time=None,
        )
        assert not _compatible(wheel, python_version="3.11", spec=spec)

    def test_too_few_dashes(self) -> None:
        """A wheel filename with too few dashes is rejected."""
        spec = PlatformSpec("linux_x86_64")
        wheel = WheelFile(
            filename="pkg-1.0.whl",
            url="",
            version="1.0",
            requires_python=None,
            has_metadata=False,
            upload_time=None,
        )
        assert not _compatible(wheel, python_version="3.11", spec=spec)

    def test_non_whl_extension_rejected(self) -> None:
        """A filename without ``.whl`` extension is rejected."""
        spec = PlatformSpec("linux_x86_64")
        sdist_like = WheelFile(
            filename="pkg-1.0.tar.gz",
            url="",
            version="1.0",
            requires_python=None,
            has_metadata=False,
            upload_time=None,
        )
        assert not _compatible(sdist_like, python_version="3.11", spec=spec)

    def test_unparseable_tag_string(self) -> None:
        """A wheel whose tag segment trips ``parse_tag`` is rejected.

        ``packaging.tags.parse_tag`` is permissive but does have a
        few error paths (e.g. an empty tag).  We patch it to raise
        and verify our ``except Exception`` handler returns None.
        """
        from nab_python.tags import _parse_tag_str

        spec = PlatformSpec("linux_x86_64")
        wheel = WheelFile(
            filename="forced-1.0-cp311-cp311-linux_x86_64.whl",
            url="",
            version="1.0",
            requires_python=None,
            has_metadata=False,
            upload_time=None,
        )
        # Clear any prior cached result on this tag suffix so the patched
        # ``parse_tag`` is exercised, and clear the None it caches so no
        # later test sees this common suffix as unparseable.
        _parse_tag_str.cache_clear()
        try:
            with patch(
                "nab_python.tags.ptags.parse_tag",
                side_effect=ValueError("forced"),
            ):
                assert not _compatible(wheel, python_version="3.11", spec=spec)
        finally:
            _parse_tag_str.cache_clear()


class TestSelectWheel:
    """Selection prefers more-specific tags."""

    def test_no_compatible_returns_none(self) -> None:
        """No compatible wheel -> None."""
        spec = PlatformSpec("linux_x86_64")
        wheels = [_wheel("pkg-1.0-cp311-cp311-win_amd64.whl")]
        assert select_wheel(wheels, python_version="3.11", spec=spec) is None

    def test_default_macos_arm64_selects_modern_only_wheel(self) -> None:
        """A version shipping only a ``macosx_12_0`` arm64 wheel is selectable."""
        spec = PlatformSpec("macos_arm64")
        wheels = [_wheel("pkg-2.2.0-cp312-cp312-macosx_12_0_arm64.whl")]
        chosen = select_wheel(wheels, python_version="3.12", spec=spec)
        assert chosen is not None
        assert chosen.filename == "pkg-2.2.0-cp312-cp312-macosx_12_0_arm64.whl"

    def test_default_macos_arm64_prefers_native_over_universal(self) -> None:
        """A ``macosx_12_0_arm64`` wheel beats the ``py3-none-any`` fallback."""
        spec = PlatformSpec("macos_arm64")
        wheels = [
            _wheel("pkg-2.2.0-py3-none-any.whl"),
            _wheel("pkg-2.2.0-cp312-cp312-macosx_12_0_arm64.whl"),
        ]
        chosen = select_wheel(wheels, python_version="3.12", spec=spec)
        assert chosen is not None
        assert chosen.filename == "pkg-2.2.0-cp312-cp312-macosx_12_0_arm64.whl"

    def test_specific_beats_universal(self) -> None:
        """A platform-specific wheel beats py3-none-any."""
        spec = PlatformSpec("linux_x86_64")
        wheels = [
            _wheel("pkg-1.0-py3-none-any.whl"),
            _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl"),
        ]
        chosen = select_wheel(wheels, python_version="3.11", spec=spec)
        assert chosen is not None
        assert "manylinux" in chosen.filename

    def test_universal_fallback(self) -> None:
        """When only py3-none-any is compatible, it wins."""
        spec = PlatformSpec("linux_x86_64")
        wheels = [_wheel("pkg-1.0-py3-none-any.whl")]
        chosen = select_wheel(wheels, python_version="3.11", spec=spec)
        assert chosen is not None
        assert chosen.filename == "pkg-1.0-py3-none-any.whl"

    def test_unparseable_wheel_skipped(self) -> None:
        """A wheel with an unparseable filename is silently skipped."""
        spec = PlatformSpec("linux_x86_64")
        good = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        wheels = [
            WheelFile(
                filename="garbage.whl",
                url="",
                version="0",
                requires_python=None,
                has_metadata=False,
                upload_time=None,
            ),
            good,
        ]
        chosen = select_wheel(wheels, python_version="3.11", spec=spec)
        assert chosen is good

    def test_higher_glibc_wheel_wins_over_lower(self) -> None:
        """Among manylinux candidates, higher glibc (closer to floor) wins.

        With floor 2.17, both manylinux_2_5 and manylinux_2_17 are
        compatible.  manylinux_2_17 is the more-specific tag (PEP 600
        recommends preferring it) and should be selected.
        """
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        wheels = [
            _wheel("pkg-1.0-cp311-cp311-manylinux_2_5_x86_64.whl"),
            _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl"),
        ]
        chosen = select_wheel(wheels, python_version="3.11", spec=spec)
        assert chosen is not None
        assert "manylinux_2_17" in chosen.filename

    def test_legacy_alias_ranks_with_its_glibc(self) -> None:
        """A legacy alias ranks at its glibc, not after all PEP 600 tags.

        manylinux2014 means glibc 2.17, so it must beat a glibc-2.5
        wheel.  packaging.tags interleaves each legacy alias right after
        its equivalent manylinux_X_Y tag, so manylinux2014 outranks
        manylinux_2_5.
        """
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        wheels = [
            _wheel("pkg-1.0-cp311-cp311-manylinux_2_5_x86_64.whl"),
            _wheel("pkg-1.0-cp311-cp311-manylinux2014_x86_64.whl"),
        ]
        chosen = select_wheel(wheels, python_version="3.11", spec=spec)
        assert chosen is not None
        assert "manylinux2014" in chosen.filename

    def test_skips_worse_candidate_after_best_set(self) -> None:
        """A later, worse-ranked wheel does not displace an earlier best.

        Reverse of the above: when manylinux_2_17 is iterated FIRST
        and manylinux_2_5 second, the loop must keep the first as
        best and not replace it.
        """
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        wheels = [
            _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl"),
            _wheel("pkg-1.0-cp311-cp311-manylinux_2_5_x86_64.whl"),
        ]
        chosen = select_wheel(wheels, python_version="3.11", spec=spec)
        assert chosen is not None
        assert "manylinux_2_17" in chosen.filename

    def test_higher_build_tag_wins_at_same_rank(self) -> None:
        """Among same-tag wheels, the higher PEP 427 build tag wins."""
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        build1 = _wheel("pkg-1.0-1-cp311-cp311-manylinux_2_17_x86_64.whl")
        build5 = _wheel("pkg-1.0-5-cp311-cp311-manylinux_2_17_x86_64.whl")
        chosen = select_wheel([build1, build5], python_version="3.11", spec=spec)
        assert chosen is build5

    def test_build_tag_selection_is_order_independent(self) -> None:
        """The same wheel is chosen regardless of index file order."""
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        build1 = _wheel("pkg-1.0-1-cp311-cp311-manylinux_2_17_x86_64.whl")
        build5 = _wheel("pkg-1.0-5-cp311-cp311-manylinux_2_17_x86_64.whl")
        forward = select_wheel([build1, build5], python_version="3.11", spec=spec)
        reverse = select_wheel([build5, build1], python_version="3.11", spec=spec)
        assert forward is build5
        assert reverse is build5

    def test_build_tagged_wheel_beats_untagged_at_same_rank(self) -> None:
        """An absent build tag sorts lowest, so a tagged wheel wins."""
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        untagged = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        build3 = _wheel("pkg-1.0-3-cp311-cp311-manylinux_2_17_x86_64.whl")
        chosen = select_wheel([untagged, build3], python_version="3.11", spec=spec)
        assert chosen is build3

    def test_malformed_build_tag_treated_as_absent(self) -> None:
        """A build segment without a leading digit sorts lowest."""
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        malformed = _wheel("pkg-1.0-x-cp311-cp311-manylinux_2_17_x86_64.whl")
        build5 = _wheel("pkg-1.0-5-cp311-cp311-manylinux_2_17_x86_64.whl")
        chosen = select_wheel([malformed, build5], python_version="3.11", spec=spec)
        assert chosen is build5


class TestUnknownPlatformKindGuard:
    """Explicit guard for the unreachable platform-kind branch."""

    def test_unknown_kind_raises_via_indirection(self) -> None:
        """Constructing a PlatformSpec with an unmapped id has no kind.

        We don't expose a way to construct one in normal use; the
        Matrix.expand validator catches unknown ids.  This test pokes
        the helper directly to cover the unreachable branch.
        """
        # Add a fake entry to test the unknown-kind branch.
        _PLATFORM_ARCH["fake_x"] = "x"
        _PLATFORM_KIND["fake_x"] = "exotic"
        try:
            spec = PlatformSpec("fake_x")
            with pytest.raises(ValueError, match="Unknown platform kind"):
                _platform_tags_for_spec(spec)
        finally:
            del _PLATFORM_ARCH["fake_x"]
            del _PLATFORM_KIND["fake_x"]


class TestPyPyTags:
    """The ``implementation="pypy"`` axis matches PyPy wheels, not CPython."""

    SPEC = PlatformSpec("linux_x86_64")

    def test_pypy_wheel_matches_pypy_tuple(self) -> None:
        """A real ``ppXY-pypyXY_pp73`` wheel matches its PyPy tuple."""
        wheel = _wheel("numpy-1.0-pp311-pypy311_pp73-manylinux_2_17_x86_64.whl")
        assert _compatible(
            wheel, python_version="3.11", spec=self.SPEC, implementation="pypy"
        )

    def test_pypy_none_wheel_matches_pypy_tuple(self) -> None:
        """A ``ppXY-none`` interpreter-specific wheel matches too."""
        wheel = _wheel("foo-1.0-pp311-none-manylinux_2_17_x86_64.whl")
        assert _compatible(
            wheel, python_version="3.11", spec=self.SPEC, implementation="pypy"
        )

    def test_cpython_wheel_rejected_on_pypy_tuple(self) -> None:
        """A CPython wheel is not a candidate for a PyPy tuple."""
        wheel = _wheel("numpy-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        assert not _compatible(
            wheel, python_version="3.11", spec=self.SPEC, implementation="pypy"
        )

    def test_pypy_wheel_rejected_on_cpython_tuple(self) -> None:
        """A PyPy wheel is not a candidate for the default CPython tuple."""
        wheel = _wheel("numpy-1.0-pp311-pypy311_pp73-manylinux_2_17_x86_64.whl")
        assert not _compatible(
            wheel, python_version="3.11", spec=self.SPEC, implementation="cpython"
        )

    def test_pypy_minor_must_match(self) -> None:
        """A PyPy 3.10 wheel does not match a PyPy 3.11 tuple."""
        wheel = _wheel("numpy-1.0-pp310-pypy310_pp73-manylinux_2_17_x86_64.whl")
        assert not _compatible(
            wheel, python_version="3.11", spec=self.SPEC, implementation="pypy"
        )

    def test_pure_python_wheel_matches_either(self) -> None:
        """A ``py3-none-any`` wheel matches both implementations."""
        wheel = _wheel("foo-1.0-py3-none-any.whl")
        assert _compatible(
            wheel, python_version="3.11", spec=self.SPEC, implementation="pypy"
        )

    def test_select_prefers_pypy_specific_over_pure_python(self) -> None:
        """The PyPy-specific wheel outranks the pure-Python fallback."""
        pure = _wheel("numpy-1.0-py3-none-any.whl")
        native = _wheel("numpy-1.0-pp311-pypy311_pp73-manylinux_2_17_x86_64.whl")
        chosen = select_wheel(
            [pure, native],
            python_version="3.11",
            spec=self.SPEC,
            implementation="pypy",
        )
        assert chosen is native

    def test_compat_tag_sets_differ_by_implementation(self) -> None:
        """The CPython and PyPy tuples accept disjoint native-tag sets."""
        cp = tags_for_target(python_version="3.11", spec=self.SPEC)
        pp = tags_for_target(
            python_version="3.11", spec=self.SPEC, implementation="pypy"
        )
        cp_native = {t for t in cp if t.abi.startswith("cp")}
        pp_native = {t for t in pp if t.abi.startswith("pypy")}
        assert cp_native
        assert pp_native
        assert cp_native.isdisjoint(pp)
        assert pp_native.isdisjoint(cp)
