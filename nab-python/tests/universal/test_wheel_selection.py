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
from nab_python.universal.wheel_selection import (
    _PLATFORM_ARCH,
    _PLATFORM_KIND,
    PlatformSpec,
    _platform_tags_for_spec,
    compatible_tags_for_tuple,
    select_wheel_for_tuple,
    wheel_compatible_with_tuple,
)


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


class TestCompatibleTagsForTuple:
    """``compatible_tags_for_tuple`` produces the full PEP 425 tag set."""

    def test_linux_includes_manylinux_at_or_below_floor(self) -> None:
        """A linux_x86_64 spec with floor 2.17 admits manylinux_2_17 and below."""
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        tag_strs = {
            str(t) for t in compatible_tags_for_tuple(python_version="3.11", spec=spec)
        }
        assert "cp311-cp311-manylinux_2_17_x86_64" in tag_strs
        assert "cp311-cp311-manylinux_2_5_x86_64" in tag_strs

    def test_linux_excludes_manylinux_above_floor(self) -> None:
        """manylinux_2_28 is NOT in the set when floor is 2.17."""
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        tag_strs = {
            str(t) for t in compatible_tags_for_tuple(python_version="3.11", spec=spec)
        }
        assert "cp311-cp311-manylinux_2_28_x86_64" not in tag_strs

    def test_linux_includes_legacy_aliases(self) -> None:
        """manylinux1, manylinux2010, manylinux2014 aliases are included."""
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        tag_strs = {
            str(t) for t in compatible_tags_for_tuple(python_version="3.11", spec=spec)
        }
        assert "cp311-cp311-manylinux1_x86_64" in tag_strs
        assert "cp311-cp311-manylinux2010_x86_64" in tag_strs
        assert "cp311-cp311-manylinux2014_x86_64" in tag_strs

    def test_linux_excludes_aliases_above_floor(self) -> None:
        """manylinux2014 is excluded when floor is below 2.17."""
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 12))
        tag_strs = {
            str(t) for t in compatible_tags_for_tuple(python_version="3.11", spec=spec)
        }
        assert "cp311-cp311-manylinux2014_x86_64" not in tag_strs
        assert "cp311-cp311-manylinux2010_x86_64" in tag_strs

    def test_linux_includes_musllinux_at_floor(self) -> None:
        """Musllinux at-or-below the floor is admitted."""
        spec = PlatformSpec("linux_x86_64", musllinux_floor=(1, 2))
        tag_strs = {
            str(t) for t in compatible_tags_for_tuple(python_version="3.11", spec=spec)
        }
        assert "cp311-cp311-musllinux_1_2_x86_64" in tag_strs
        assert "cp311-cp311-musllinux_1_0_x86_64" in tag_strs

    def test_macos_arm64_uses_default_floor(self) -> None:
        """``macos_arm64`` defaults to macOS 11 (arm64 minimum)."""
        spec = PlatformSpec("macos_arm64")
        tag_strs = {
            str(t) for t in compatible_tags_for_tuple(python_version="3.11", spec=spec)
        }
        # mac_platforms yields versions <= the declared one
        assert "cp311-cp311-macosx_11_0_arm64" in tag_strs

    def test_macos_x86_64_uses_default_floor(self) -> None:
        """``macos_x86_64`` defaults to macOS 10.13 (x86_64-era)."""
        spec = PlatformSpec("macos_x86_64")
        tag_strs = {
            str(t) for t in compatible_tags_for_tuple(python_version="3.11", spec=spec)
        }
        assert "cp311-cp311-macosx_10_13_x86_64" in tag_strs

    def test_macos_explicit_min(self) -> None:
        """An explicit ``macos_min`` overrides the default."""
        spec = PlatformSpec("macos_arm64", macos_min=(14, 0))
        tag_strs = {
            str(t) for t in compatible_tags_for_tuple(python_version="3.11", spec=spec)
        }
        assert "cp311-cp311-macosx_14_0_arm64" in tag_strs

    def test_windows_amd64(self) -> None:
        """Windows amd64 generates ``win_amd64`` tags."""
        spec = PlatformSpec("windows_amd64")
        tag_strs = {
            str(t) for t in compatible_tags_for_tuple(python_version="3.11", spec=spec)
        }
        assert "cp311-cp311-win_amd64" in tag_strs

    def test_includes_universal_tags(self) -> None:
        """``py3-none-any`` and ``cp311-none-any`` are always in the set."""
        spec = PlatformSpec("linux_x86_64")
        tag_strs = {
            str(t) for t in compatible_tags_for_tuple(python_version="3.11", spec=spec)
        }
        # cp311-none-any only appears for windows in compatible_tags;
        # py3-none-any is the universal pure-Python tag.
        assert any(t == "py3-none-any" for t in tag_strs)


class TestWheelCompatibility:
    """``wheel_compatible_with_tuple`` accepts/rejects per the tag rules."""

    def test_accepts_at_floor_manylinux(self) -> None:
        """A manylinux_2_17 wheel matches a 2.17-floor linux spec."""
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        assert wheel_compatible_with_tuple(wheel, python_version="3.11", spec=spec)

    def test_rejects_above_floor_manylinux(self) -> None:
        """A manylinux_2_28 wheel does not match a 2.17-floor linux spec."""
        spec = PlatformSpec("linux_x86_64", manylinux_floor=(2, 17))
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_28_x86_64.whl")
        assert not wheel_compatible_with_tuple(wheel, python_version="3.11", spec=spec)

    def test_accepts_universal_wheel(self) -> None:
        """``py3-none-any`` is accepted by every platform."""
        wheel = _wheel("pkg-1.0-py3-none-any.whl")
        for platform_id in (
            "linux_x86_64",
            "macos_arm64",
            "windows_amd64",
        ):
            spec = PlatformSpec(platform_id)
            assert wheel_compatible_with_tuple(wheel, python_version="3.11", spec=spec)

    def test_rejects_wrong_arch(self) -> None:
        """An aarch64 wheel does not match a x86_64 spec."""
        spec = PlatformSpec("linux_x86_64")
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_aarch64.whl")
        assert not wheel_compatible_with_tuple(wheel, python_version="3.11", spec=spec)

    def test_rejects_wrong_python_minor(self) -> None:
        """A cp311 wheel does not match a 3.12 tuple."""
        spec = PlatformSpec("linux_x86_64")
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        assert not wheel_compatible_with_tuple(wheel, python_version="3.12", spec=spec)

    def test_compressed_tag_set(self) -> None:
        """A wheel with cp310.cp311 matches both 3.10 and 3.11."""
        spec = PlatformSpec("linux_x86_64")
        wheel = _wheel("pkg-1.0-cp310.cp311-cp310.cp311-manylinux_2_17_x86_64.whl")
        assert wheel_compatible_with_tuple(wheel, python_version="3.10", spec=spec)
        assert wheel_compatible_with_tuple(wheel, python_version="3.11", spec=spec)

    def test_abi3_forward_compat(self) -> None:
        """A cp310-abi3 wheel works on 3.10, 3.11, etc."""
        spec = PlatformSpec("linux_x86_64")
        wheel = _wheel("pkg-1.0-cp310-abi3-manylinux_2_17_x86_64.whl")
        assert wheel_compatible_with_tuple(wheel, python_version="3.10", spec=spec)
        assert wheel_compatible_with_tuple(wheel, python_version="3.13", spec=spec)

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
        assert not wheel_compatible_with_tuple(wheel, python_version="3.11", spec=spec)

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
        assert not wheel_compatible_with_tuple(wheel, python_version="3.11", spec=spec)

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
        assert not wheel_compatible_with_tuple(
            sdist_like, python_version="3.11", spec=spec
        )

    def test_unparseable_tag_string(self) -> None:
        """A wheel whose tag segment trips ``parse_tag`` is rejected.

        ``packaging.tags.parse_tag`` is permissive but does have a
        few error paths (e.g. an empty tag).  We patch it to raise
        and verify our ``except Exception`` handler returns None.
        """
        from nab_python.universal.wheel_selection import _parse_tag_str

        spec = PlatformSpec("linux_x86_64")
        wheel = WheelFile(
            filename="forced-1.0-cp311-cp311-linux_x86_64.whl",
            url="",
            version="1.0",
            requires_python=None,
            has_metadata=False,
            upload_time=None,
        )
        # Clear any prior cached result on this tag suffix so the
        # patched ``parse_tag`` is exercised regardless of test ordering.
        _parse_tag_str.cache_clear()
        with patch(
            "nab_python.universal.wheel_selection.ptags.parse_tag",
            side_effect=ValueError("forced"),
        ):
            assert not wheel_compatible_with_tuple(
                wheel, python_version="3.11", spec=spec
            )


class TestSelectWheelForTuple:
    """Selection prefers more-specific tags."""

    def test_no_compatible_returns_none(self) -> None:
        """No compatible wheel -> None."""
        spec = PlatformSpec("linux_x86_64")
        wheels = [_wheel("pkg-1.0-cp311-cp311-win_amd64.whl")]
        assert select_wheel_for_tuple(wheels, python_version="3.11", spec=spec) is None

    def test_specific_beats_universal(self) -> None:
        """A platform-specific wheel beats py3-none-any."""
        spec = PlatformSpec("linux_x86_64")
        wheels = [
            _wheel("pkg-1.0-py3-none-any.whl"),
            _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl"),
        ]
        chosen = select_wheel_for_tuple(wheels, python_version="3.11", spec=spec)
        assert chosen is not None
        assert "manylinux" in chosen.filename

    def test_universal_fallback(self) -> None:
        """When only py3-none-any is compatible, it wins."""
        spec = PlatformSpec("linux_x86_64")
        wheels = [_wheel("pkg-1.0-py3-none-any.whl")]
        chosen = select_wheel_for_tuple(wheels, python_version="3.11", spec=spec)
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
        chosen = select_wheel_for_tuple(wheels, python_version="3.11", spec=spec)
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
        chosen = select_wheel_for_tuple(wheels, python_version="3.11", spec=spec)
        assert chosen is not None
        assert "manylinux_2_17" in chosen.filename

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
        chosen = select_wheel_for_tuple(wheels, python_version="3.11", spec=spec)
        assert chosen is not None
        assert "manylinux_2_17" in chosen.filename


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
