"""Tests for the wheel-selection logic backed by ``packaging.tags``.

These tests pin the spec-compliant tag selector's behavior on the
common cases that real resolvers encounter:

- one libc family per target, at or below its version (PEP 600 / PEP 656)
- macOS arch + version compatibility
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
    _tags_in_order,
    select_wheel,
    tags_for_target,
)

# numpy 2.5.1 ships one manylinux wheel (tagged 2.27 and 2.28, no older),
# one musllinux wheel, and one free-threaded wheel per platform.
NUMPY_MANYLINUX = (
    "numpy-2.5.1-cp313-cp313-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"
)
NUMPY_MUSLLINUX = "numpy-2.5.1-cp313-cp313-musllinux_1_2_x86_64.whl"
NUMPY_FREETHREADED = (
    "numpy-2.5.1-cp313-cp313t-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"
)
# cryptography ships one abi3 wheel per platform, usable from cp37 up.
CRYPTOGRAPHY_ABI3 = "cryptography-44.0.0-cp37-abi3-manylinux_2_28_x86_64.whl"


def _free_threaded_host() -> object:
    """Patch the config vars packaging reads to fake a free-threaded host."""
    return patch(
        "nab_python.tags.ptags._get_config_var",
        side_effect=lambda name, warn=False: 1 if name == "Py_GIL_DISABLED" else None,
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


class TestPlatformSpecKnobsBelongToTheirPlatform:
    """A knob the platform cannot read is a construction error.

    The tag generator consults a libc only on Linux and a macOS version
    only on macOS, so a knob on any other platform selects no different
    wheel.  It does reach the target's label, so accepting one would name
    a machine that was never modelled.
    """

    @pytest.mark.parametrize("platform_id", ["macos_arm64", "windows_amd64"])
    def test_libc_outside_linux_raises(self, platform_id: str) -> None:
        """Only a Linux target links a C library."""
        with pytest.raises(ValueError, match="libc is a Linux knob"):
            PlatformSpec(platform_id, libc="musl")

    @pytest.mark.parametrize("platform_id", ["macos_arm64", "windows_amd64"])
    def test_libc_version_outside_linux_raises(self, platform_id: str) -> None:
        """A libc version is as Linux-only as the family it versions."""
        with pytest.raises(ValueError, match="libc is a Linux knob"):
            PlatformSpec(platform_id, libc_version=(2, 28))

    @pytest.mark.parametrize("platform_id", ["linux_x86_64", "windows_amd64"])
    def test_macos_min_outside_macos_raises(self, platform_id: str) -> None:
        """A deployment target means nothing off macOS."""
        with pytest.raises(ValueError, match="macos-min is a macOS knob"):
            PlatformSpec(platform_id, macos_min=(14, 0))

    @pytest.mark.parametrize(
        ("platform_id", "macos_min", "floor"),
        [
            ("macos_x86_64", (10, 3), "10.4"),
            ("macos_arm64", (10, 15), "11.0"),
        ],
    )
    def test_macos_min_below_the_arch_floor_raises(
        self, platform_id: str, macos_min: tuple[int, int], floor: str
    ) -> None:
        """No wheel tag names a macOS older than the arch itself.

        ``mac_platforms`` yields no x86_64 binary format below 10.4, and
        Apple Silicon shipped at 11.0.  An empty platform list also reads to
        packaging as "unset", which would hand the target the tags of the
        host nab happens to be running on.
        """
        with pytest.raises(ValueError, match=f"below {floor}"):
            PlatformSpec(platform_id, macos_min=macos_min)

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"platform_id": "linux_x86_64", "libc_version": (2, 500)}, "libc-version"),
            ({"platform_id": "macos_arm64", "macos_min": (500, 0)}, "macos-min"),
        ],
    )
    def test_a_version_above_every_release_raises(
        self, kwargs: dict[str, object], message: str
    ) -> None:
        """One tag is named per version below the declared one.

        A typo of a few extra digits would otherwise build platform tags
        until the process ran out of memory.
        """
        with pytest.raises(ValueError, match=f"{message}.*higher than any release"):
            PlatformSpec(**kwargs)  # type: ignore[arg-type]

    def test_unknown_platform_id_defers_to_the_matrix(self) -> None:
        """An unknown id is the matrix's error to report, not the spec's."""
        assert PlatformSpec("freebsd_amd64", libc="musl").libc == "musl"


class TestPlatformSpec:
    """``PlatformSpec`` exposes per-platform tag knobs."""

    def test_default_libc_is_glibc_2_28(self) -> None:
        """An undeclared Linux target is glibc 2.28."""
        spec = PlatformSpec("linux_x86_64")
        assert spec.libc == "glibc"
        assert spec.effective_libc_version == (2, 28)

    def test_musl_libc_version_defaults_per_family(self) -> None:
        """A musl target with no version takes musl's default, not glibc's."""
        spec = PlatformSpec("linux_x86_64", libc="musl")
        assert spec.effective_libc_version == (1, 2)

    def test_declared_libc_version_wins(self) -> None:
        """An explicit ``libc_version`` overrides the family default."""
        spec = PlatformSpec("linux_x86_64", libc_version=(2, 17))
        assert spec.effective_libc_version == (2, 17)

    def test_label_of_default_spec_is_the_id(self) -> None:
        """A spec left at the platform defaults renders as its id."""
        assert PlatformSpec("linux_x86_64").label == "linux_x86_64"

    def test_label_carries_the_knobs(self) -> None:
        """A spec off the defaults renders its id plus what sets it apart."""
        spec = PlatformSpec("linux_x86_64", libc="musl")
        assert spec.label == "linux_x86_64-musl"

    def test_label_names_the_libc_family(self) -> None:
        """A musl target never renders the label of a glibc one.

        The two families' version numbers are not comparable, so a label
        that dropped the family could collapse two targets with disjoint
        wheel sets onto one key.
        """
        glibc = PlatformSpec("linux_x86_64", libc_version=(2, 17))
        musl = PlatformSpec("linux_x86_64", libc="musl", libc_version=(1, 1))
        assert glibc.label == "linux_x86_64-glibc2.17"
        assert musl.label == "linux_x86_64-musl1.1"

    def test_default_linux_arch(self) -> None:
        """``linux_x86_64`` carries the right architecture name."""
        spec = PlatformSpec("linux_x86_64")
        assert spec.arch == "x86_64"

    def test_macos_arch(self) -> None:
        """``macos_arm64`` arch is ``arm64``."""
        spec = PlatformSpec("macos_arm64")
        assert spec.arch == "arm64"

    def test_label_distinguishes_space_from_underscore(self) -> None:
        """Whitespace and ``_`` in ``platform_release`` encode differently.

        Both are legal release characters; if they folded together the two
        specs' targets would share a label and their pins would merge.
        """
        with_space = PlatformSpec("linux_x86_64", platform_release="a a")
        with_underscore = PlatformSpec("linux_x86_64", platform_release="a_a")
        assert with_space.label != with_underscore.label

    def test_label_release_cannot_forge_a_version_field(self) -> None:
        """A ``-`` in ``platform_release`` cannot fake a ``ver`` field."""
        release_only = PlatformSpec("linux_x86_64", platform_release="r-ver1")
        release_and_version = PlatformSpec(
            "linux_x86_64", platform_release="r", platform_version="1"
        )
        assert release_only.label != release_and_version.label

    def test_label_escapes_a_kernel_release(self) -> None:
        """Pins the escaped label shape for a realistic kernel release."""
        spec = PlatformSpec("linux_x86_64", platform_release="5.15.0-generic")
        assert spec.label == "linux_x86_64-rel5.15.0_2d_generic"


class TestLibcFamilyExclusivity:
    """A target links one C library, so it accepts one family's wheels."""

    def test_glibc_target_emits_no_musllinux(self) -> None:
        """The default (glibc) linux target has no musllinux platform tag."""
        platforms = _platform_tags_for_spec(PlatformSpec("linux_x86_64"))
        assert any(p.startswith("manylinux") for p in platforms)
        assert not any(p.startswith("musllinux") for p in platforms)

    def test_musl_target_emits_no_manylinux(self) -> None:
        """A musl target has no manylinux platform tag."""
        spec = PlatformSpec("linux_x86_64", libc="musl")
        platforms = _platform_tags_for_spec(spec)
        assert any(p.startswith("musllinux") for p in platforms)
        assert not any(p.startswith("manylinux") for p in platforms)

    def test_glibc_target_takes_numpy_manylinux_wheel(self) -> None:
        """numpy ships manylinux_2_28 only; the default glibc target installs it."""
        spec = PlatformSpec("linux_x86_64")
        assert _compatible(_wheel(NUMPY_MANYLINUX), python_version="3.13", spec=spec)

    def test_glibc_target_rejects_numpy_musllinux_wheel(self) -> None:
        """The same glibc target must not pick numpy's musl wheel."""
        spec = PlatformSpec("linux_x86_64")
        assert not _compatible(
            _wheel(NUMPY_MUSLLINUX), python_version="3.13", spec=spec
        )

    def test_musl_target_takes_numpy_musllinux_wheel(self) -> None:
        """A musl target installs numpy's musllinux wheel."""
        spec = PlatformSpec("linux_x86_64", libc="musl")
        assert _compatible(_wheel(NUMPY_MUSLLINUX), python_version="3.13", spec=spec)

    def test_musl_target_rejects_numpy_manylinux_wheel(self) -> None:
        """A musl target must not pick numpy's glibc wheel."""
        spec = PlatformSpec("linux_x86_64", libc="musl")
        assert not _compatible(
            _wheel(NUMPY_MANYLINUX), python_version="3.13", spec=spec
        )

    def test_glibc_target_selects_manylinux_over_musllinux(self) -> None:
        """Given numpy's full wheel list, the glibc target picks the glibc wheel."""
        spec = PlatformSpec("linux_x86_64")
        wheels = [_wheel(NUMPY_MUSLLINUX), _wheel(NUMPY_MANYLINUX)]
        chosen = select_wheel(wheels, python_version="3.13", spec=spec)
        assert chosen is not None
        assert chosen.filename == NUMPY_MANYLINUX


class TestFreeThreadedTarget:
    """``free_threaded`` declares the ``cpXYt`` ABI, which is exclusive.

    A free-threaded interpreter loads neither an ordinary ``cpXY``
    extension nor an ``abi3`` one, and a GIL interpreter cannot load a
    ``cpXYt`` one, so the two targets take disjoint binary wheels.
    """

    def test_free_threaded_target_takes_the_cp313t_wheel(self) -> None:
        """numpy's cp313t wheel is a candidate for a free-threaded target."""
        spec = PlatformSpec("linux_x86_64", free_threaded=True)
        assert _compatible(_wheel(NUMPY_FREETHREADED), python_version="3.13", spec=spec)

    def test_free_threaded_target_rejects_the_gil_wheel(self) -> None:
        """The same target must not pick numpy's ordinary cp313 wheel."""
        spec = PlatformSpec("linux_x86_64", free_threaded=True)
        assert not _compatible(
            _wheel(NUMPY_MANYLINUX), python_version="3.13", spec=spec
        )

    def test_free_threaded_target_rejects_abi3(self) -> None:
        """abi3 wheels do not load on a free-threaded build (PEP 703)."""
        spec = PlatformSpec("linux_x86_64", free_threaded=True)
        assert not _compatible(
            _wheel(CRYPTOGRAPHY_ABI3), python_version="3.13", spec=spec
        )

    def test_free_threaded_target_keeps_abi3t_and_pure_python(self) -> None:
        """The stable free-threaded ABI and the pure-Python fallback remain."""
        spec = PlatformSpec("linux_x86_64", free_threaded=True)
        tag_strs = {str(t) for t in tags_for_target(python_version="3.13", spec=spec)}
        assert "cp313-abi3t-manylinux_2_28_x86_64" in tag_strs
        assert "py3-none-any" in tag_strs

    def test_gil_target_rejects_the_free_threaded_wheel(self) -> None:
        """The default (GIL) target must not pick numpy's cp313t wheel."""
        spec = PlatformSpec("linux_x86_64")
        assert not _compatible(
            _wheel(NUMPY_FREETHREADED), python_version="3.13", spec=spec
        )

    def test_label_carries_the_free_threaded_discriminator(self) -> None:
        """A free-threaded target renders its own label."""
        spec = PlatformSpec("linux_x86_64", free_threaded=True)
        assert spec.label == "linux_x86_64-ft"


class TestAbiIsHostIndependent:
    """The ABI comes from the declared target, not from the running host.

    packaging derives ABIs from the host's ``Py_GIL_DISABLED`` /
    ``Py_DEBUG`` config vars when it is not told which ABI to use.  nab
    runs on any interpreter, so a host-derived ABI would make the same
    matrix lock differently under a free-threaded or debug build.
    """

    def test_gil_target_unchanged_on_a_free_threaded_host(self) -> None:
        """A declared GIL target keeps its cp313 and abi3 tags on such a host."""
        spec = PlatformSpec("linux_x86_64")
        with _free_threaded_host():
            tag_strs = {str(t) for t in _tags_in_order("3.13", spec)}
        assert "cp313-cp313-manylinux_2_28_x86_64" in tag_strs
        assert "cp313-abi3-manylinux_2_28_x86_64" in tag_strs
        assert not any("cp313t" in t for t in tag_strs)

    def test_ordinary_wheels_stay_candidates_on_a_free_threaded_host(self) -> None:
        """The cp313 and abi3 wheels a GIL target installs stay selectable."""
        spec = PlatformSpec("linux_x86_64")
        wheels = [_wheel(NUMPY_MANYLINUX), _wheel(CRYPTOGRAPHY_ABI3)]
        with _free_threaded_host():
            for wheel in wheels:
                assert _compatible(wheel, python_version="3.13", spec=spec)
            assert not _compatible(
                _wheel(NUMPY_FREETHREADED), python_version="3.13", spec=spec
            )

    def test_free_threaded_target_reachable_from_any_host(self) -> None:
        """A declared free-threaded target gets its cp313t tag with no host help."""
        spec = PlatformSpec("linux_x86_64", free_threaded=True)
        tag_strs = {str(t) for t in _tags_in_order("3.13", spec)}
        assert "cp313-cp313t-manylinux_2_28_x86_64" in tag_strs


class TestTagsForTarget:
    """``tags_for_target`` produces the full PEP 425 tag set."""

    def test_linux_includes_manylinux_at_or_below_libc_version(self) -> None:
        """A glibc 2.17 target admits manylinux_2_17 and below."""
        spec = PlatformSpec("linux_x86_64", libc_version=(2, 17))
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "cp311-cp311-manylinux_2_17_x86_64" in tag_strs
        assert "cp311-cp311-manylinux_2_5_x86_64" in tag_strs

    def test_linux_excludes_manylinux_above_libc_version(self) -> None:
        """manylinux_2_28 needs glibc 2.28; a 2.17 target cannot run it."""
        spec = PlatformSpec("linux_x86_64", libc_version=(2, 17))
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "cp311-cp311-manylinux_2_28_x86_64" not in tag_strs

    def test_default_linux_admits_manylinux_2_28(self) -> None:
        """The default glibc version admits manylinux_2_28."""
        spec = PlatformSpec("linux_x86_64")
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "cp311-cp311-manylinux_2_28_x86_64" in tag_strs

    def test_linux_includes_legacy_aliases(self) -> None:
        """manylinux1, manylinux2010, manylinux2014 aliases are included."""
        spec = PlatformSpec("linux_x86_64", libc_version=(2, 17))
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "cp311-cp311-manylinux1_x86_64" in tag_strs
        assert "cp311-cp311-manylinux2010_x86_64" in tag_strs
        assert "cp311-cp311-manylinux2014_x86_64" in tag_strs

    def test_linux_excludes_aliases_above_libc_version(self) -> None:
        """manylinux2014 means glibc 2.17; a 2.12 target excludes it."""
        spec = PlatformSpec("linux_x86_64", libc_version=(2, 12))
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "cp311-cp311-manylinux2014_x86_64" not in tag_strs
        assert "cp311-cp311-manylinux2010_x86_64" in tag_strs

    def test_aarch64_manylinux_stops_at_2_17(self) -> None:
        """aarch64 does not descend below glibc 2.17 (PEP 599)."""
        spec = PlatformSpec("linux_aarch64", libc_version=(2, 17))
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

    def test_x86_64_keeps_legacy_alias_range(self) -> None:
        """x86_64 still descends to manylinux1 (glibc 2.5)."""
        spec = PlatformSpec("linux_x86_64", libc_version=(2, 17))
        tag_strs = {str(t) for t in tags_for_target(python_version="3.11", spec=spec)}
        assert "cp311-cp311-manylinux1_x86_64" in tag_strs
        assert "cp311-cp311-manylinux_2_5_x86_64" in tag_strs

    def test_musl_includes_musllinux_at_or_below_version(self) -> None:
        """A musl 1.2 target admits musllinux_1_2 and below."""
        spec = PlatformSpec("linux_x86_64", libc="musl", libc_version=(1, 2))
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

    def test_macos_x86_64_uses_default_min(self) -> None:
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

    def test_accepts_manylinux_at_libc_version(self) -> None:
        """A manylinux_2_17 wheel matches a glibc 2.17 target."""
        spec = PlatformSpec("linux_x86_64", libc_version=(2, 17))
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        assert _compatible(wheel, python_version="3.11", spec=spec)

    def test_rejects_manylinux_above_libc_version(self) -> None:
        """A manylinux_2_28 wheel does not match a glibc 2.17 target."""
        spec = PlatformSpec("linux_x86_64", libc_version=(2, 17))
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
        # Clear before, so the patched ``parse_tag`` runs, and after, so no
        # later test sees this suffix cached as unparseable.
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
        """Among manylinux candidates, the highest runnable glibc wins.

        On a glibc 2.17 target both manylinux_2_5 and manylinux_2_17 are
        compatible.  manylinux_2_17 is the more-specific tag (PEP 600
        recommends preferring it) and should be selected.
        """
        spec = PlatformSpec("linux_x86_64", libc_version=(2, 17))
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
        spec = PlatformSpec("linux_x86_64", libc_version=(2, 17))
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
        spec = PlatformSpec("linux_x86_64", libc_version=(2, 17))
        wheels = [
            _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl"),
            _wheel("pkg-1.0-cp311-cp311-manylinux_2_5_x86_64.whl"),
        ]
        chosen = select_wheel(wheels, python_version="3.11", spec=spec)
        assert chosen is not None
        assert "manylinux_2_17" in chosen.filename

    def test_higher_build_tag_wins_at_same_rank(self) -> None:
        """Among same-tag wheels, the higher PEP 427 build tag wins."""
        spec = PlatformSpec("linux_x86_64", libc_version=(2, 17))
        build1 = _wheel("pkg-1.0-1-cp311-cp311-manylinux_2_17_x86_64.whl")
        build5 = _wheel("pkg-1.0-5-cp311-cp311-manylinux_2_17_x86_64.whl")
        chosen = select_wheel([build1, build5], python_version="3.11", spec=spec)
        assert chosen is build5

    def test_build_tag_selection_is_order_independent(self) -> None:
        """The same wheel is chosen regardless of index file order."""
        spec = PlatformSpec("linux_x86_64", libc_version=(2, 17))
        build1 = _wheel("pkg-1.0-1-cp311-cp311-manylinux_2_17_x86_64.whl")
        build5 = _wheel("pkg-1.0-5-cp311-cp311-manylinux_2_17_x86_64.whl")
        forward = select_wheel([build1, build5], python_version="3.11", spec=spec)
        reverse = select_wheel([build5, build1], python_version="3.11", spec=spec)
        assert forward is build5
        assert reverse is build5

    def test_build_tagged_wheel_beats_untagged_at_same_rank(self) -> None:
        """An absent build tag sorts lowest, so a tagged wheel wins."""
        spec = PlatformSpec("linux_x86_64", libc_version=(2, 17))
        untagged = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        build3 = _wheel("pkg-1.0-3-cp311-cp311-manylinux_2_17_x86_64.whl")
        chosen = select_wheel([untagged, build3], python_version="3.11", spec=spec)
        assert chosen is build3

    def test_malformed_build_tag_treated_as_absent(self) -> None:
        """A build segment without a leading digit sorts lowest."""
        spec = PlatformSpec("linux_x86_64", libc_version=(2, 17))
        malformed = _wheel("pkg-1.0-x-cp311-cp311-manylinux_2_17_x86_64.whl")
        build5 = _wheel("pkg-1.0-5-cp311-cp311-manylinux_2_17_x86_64.whl")
        chosen = select_wheel([malformed, build5], python_version="3.11", spec=spec)
        assert chosen is build5


class TestUnknownPlatformKindGuard:
    """A platform kind with no tag rule raises rather than tagging as Windows."""

    def test_unknown_kind_raises(self) -> None:
        """A kind the tag generator does not handle is a loud error.

        No id maps to a fourth kind today, so the branch is reachable only
        by adding one, which is what this test stands in for.
        """
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
