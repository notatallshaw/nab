"""Tests for ``nab_provider.tags``: the tags a target accepts, and the wheel it picks.

Pins:

- the knobs a platform can carry, and the ones it refuses
- one libc family per target, at or below a set version (PEP 600 / PEP 656)
- an unset runs-on-libc/runs-on-macos accepts any level; a set one drops wheels
  needing newer, per floor
- the free-threaded ``cpXYt`` ABI, and ``abi3t`` in place of ``abi3``
- ``py3-none-any`` fallback ordering
- compressed-tag-set wheels (``cp310.cp311``)
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from unittest.mock import MagicMock, patch

import pytest

from nab_provider._vendor.packaging.tags import Tag
from nab_provider.records import WheelFile
from nab_provider.tags import (
    _PLATFORM_ARCH,
    _PLATFORM_KIND,
    Libc,
    PlatformSpec,
    TagSet,
    _packaging_tags,
    _parse_tag_str,
    _platform_tags_for_spec,
    _tags_in_order,
    python_axis_accepts,
    wheel_tag_set,
)
from nab_provider.target import PLATFORM_MARKERS

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


def _free_threaded_host() -> AbstractContextManager[MagicMock]:
    """Patch the config vars packaging reads to fake a free-threaded host."""
    return patch(
        "nab_provider.tags.ptags._get_config_var",
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
    chosen = TagSet.for_spec(
        python_version=python_version, spec=spec, implementation=implementation
    ).pick([wheel])
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

    @pytest.mark.parametrize(
        "platform_id", ["macos_arm64", "windows_amd64", "windows_arm64"]
    )
    def test_libc_outside_linux_raises(self, platform_id: str) -> None:
        """Only a Linux target links a C library."""
        with pytest.raises(ValueError, match="libc and runs-on-libc are Linux knobs"):
            PlatformSpec(platform_id, libc="musl")

    @pytest.mark.parametrize("platform_id", ["macos_arm64", "windows_amd64"])
    def test_runs_on_libc_outside_linux_raises(self, platform_id: str) -> None:
        """A libc version is as Linux-only as the family it versions."""
        with pytest.raises(ValueError, match="libc and runs-on-libc are Linux knobs"):
            PlatformSpec(platform_id, runs_on_libc=(2, 28))

    @pytest.mark.parametrize("platform_id", ["linux_x86_64", "windows_amd64"])
    def test_runs_on_macos_outside_macos_raises(self, platform_id: str) -> None:
        """The macOS a lock runs on means nothing off macOS."""
        with pytest.raises(ValueError, match="runs-on-macos is a macOS knob"):
            PlatformSpec(platform_id, runs_on_macos=(14, 0))

    @pytest.mark.parametrize(
        ("platform_id", "runs_on_macos", "floor"),
        [
            ("macos_x86_64", (10, 3), "10.4"),
            ("macos_arm64", (10, 15), "11.0"),
        ],
    )
    def test_runs_on_macos_below_the_arch_floor_raises(
        self, platform_id: str, runs_on_macos: tuple[int, int], floor: str
    ) -> None:
        """No wheel tag names a macOS older than the arch itself.

        ``mac_platforms`` yields no x86_64 binary format below 10.4, and
        Apple Silicon shipped at 11.0.  An empty platform list also reads to
        packaging as "unset", which would hand the target the tags of the
        host nab happens to be running on.
        """
        with pytest.raises(ValueError, match=f"below {floor}"):
            PlatformSpec(platform_id, runs_on_macos=runs_on_macos)

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"platform_id": "linux_x86_64", "runs_on_libc": (2, 500)}, "runs-on-libc"),
            (
                {"platform_id": "macos_arm64", "runs_on_macos": (500, 0)},
                "runs-on-macos",
            ),
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

    @pytest.mark.parametrize(
        ("libc", "runs_on_libc", "message"),
        [
            ("musl", (2, 5), "musl has only a 1.x series"),
            ("glibc", (1, 2), "glibc has only a 2.x series"),
        ],
    )
    def test_a_libc_version_from_the_other_family_raises(
        self, libc: Libc, runs_on_libc: tuple[int, int], message: str
    ) -> None:
        """Each family versions one major series, so the other names no tag."""
        with pytest.raises(ValueError, match=message):
            PlatformSpec("linux_x86_64", libc=libc, runs_on_libc=runs_on_libc)

    def test_unknown_platform_id_defers_to_the_matrix(self) -> None:
        """An unknown id is the matrix's error to report, not the spec's."""
        assert PlatformSpec("freebsd_amd64", libc="musl").libc == "musl"


class TestPlatformSpec:
    """``PlatformSpec`` exposes per-platform tag knobs."""

    def test_undeclared_linux_target_is_glibc_accepting_any_level(self) -> None:
        """An undeclared Linux target is glibc with no runs-on-libc, so any level."""
        spec = PlatformSpec("linux_x86_64")
        assert spec.libc == "glibc"
        assert spec.runs_on_libc is None

    def test_musl_target_carries_no_default_version(self) -> None:
        """A musl target with no runs-on-libc accepts any level too."""
        spec = PlatformSpec("linux_x86_64", libc="musl")
        assert spec.libc == "musl"
        assert spec.runs_on_libc is None

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
        glibc = PlatformSpec("linux_x86_64", runs_on_libc=(2, 17))
        musl = PlatformSpec("linux_x86_64", libc="musl", runs_on_libc=(1, 1))
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

    def test_windows_arm64_arch(self) -> None:
        """``windows_arm64`` wheel-tag arch is the lowercase ``arm64``."""
        assert PlatformSpec("windows_arm64").arch == "arm64"

    def test_linux_i686_arch(self) -> None:
        """``linux_i686`` carries the ``i686`` arch."""
        assert PlatformSpec("linux_i686").arch == "i686"

    def test_linux_armv7l_arch(self) -> None:
        """``linux_armv7l`` carries the ``armv7l`` arch."""
        assert PlatformSpec("linux_armv7l").arch == "armv7l"

    def test_armv7l_target_carries_no_default_version(self) -> None:
        """An undeclared armv7l target sets no runs-on-libc, so it accepts any level.

        Its manylinux_2_31 wheels, and any newer, are all admitted.
        """
        spec = PlatformSpec("linux_armv7l")
        assert spec.libc == "glibc"
        assert spec.runs_on_libc is None

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
        chosen = TagSet.for_spec(python_version="3.13", spec=spec).pick(wheels)
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
        tag_strs = {
            str(t) for t in TagSet.for_spec(python_version="3.13", spec=spec).members
        }
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


class TestPymallocAbi:
    """CPython carried the pymalloc flag in its ABI tag through 3.7."""

    def test_a_37_target_names_the_m_abi(self) -> None:
        """Every 3.7 wheel is tagged cp37m, so a cp37 target matches none."""
        spec = PlatformSpec("linux_x86_64")
        tags = TagSet.for_spec(python_version="3.7", spec=spec)
        assert "cp37m" in {t.abi for t in tags.ordered}
        assert tags.accepts(
            "numpy-1.21.6-cp37-cp37m-manylinux_2_12_x86_64.manylinux2010_x86_64.whl"
        )

    def test_38_dropped_the_flag(self) -> None:
        spec = PlatformSpec("linux_x86_64")
        tags = TagSet.for_spec(python_version="3.8", spec=spec)
        assert "cp38m" not in {t.abi for t in tags.ordered}
        assert tags.accepts("numpy-1.24.4-cp38-cp38-manylinux_2_17_x86_64.whl")


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
            tags = TagSet.for_spec(python_version="3.13", spec=spec)
            tag_strs = {str(t) for t in tags.ordered}
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
        tags = TagSet.for_spec(python_version="3.13", spec=spec)
        tag_strs = {str(t) for t in tags.ordered}
        assert "cp313-cp313t-manylinux_2_28_x86_64" in tag_strs


class TestPlatformIdTableParity:
    """The three id-keyed platform tables must name the same ids.

    ``Matrix.expand`` validates a declared id against ``PLATFORM_MARKERS``
    only, while ``PlatformSpec.arch`` and ``platform_kind`` read the other
    two.  An id in one table but not the others would pass validation and
    then ``KeyError`` deep in tag building.
    """

    def test_the_three_tables_agree(self) -> None:
        assert set(PLATFORM_MARKERS) == set(_PLATFORM_ARCH) == set(_PLATFORM_KIND)


class TestWindowsArm64:
    """``windows_arm64`` names the ``win_arm64`` wheel tag."""

    def test_platform_tag_is_win_arm64(self) -> None:
        assert _platform_tags_for_spec(PlatformSpec("windows_arm64")) == ["win_arm64"]

    def test_accepts_a_win_arm64_wheel(self) -> None:
        spec = PlatformSpec("windows_arm64")
        wheel = _wheel("foo-1.0-cp312-cp312-win_arm64.whl")
        assert _compatible(wheel, python_version="3.12", spec=spec)

    def test_free_threaded_target_takes_the_cp313t_wheel(self) -> None:
        spec = PlatformSpec("windows_arm64", free_threaded=True)
        wheel = _wheel("foo-1.0-cp313-cp313t-win_arm64.whl")
        assert _compatible(wheel, python_version="3.13", spec=spec)


class TestLinuxI686:
    """``linux_i686`` emits the i686 manylinux family down to glibc 2.5."""

    def test_default_target_emits_manylinux_i686(self) -> None:
        platforms = _platform_tags_for_spec(PlatformSpec("linux_i686"))
        assert platforms[0] == "linux_i686"
        assert "manylinux_2_28_i686" in platforms
        assert "manylinux_2_5_i686" in platforms
        assert not any(p.startswith("musllinux") for p in platforms)

    def test_accepts_a_manylinux2014_i686_wheel(self) -> None:
        spec = PlatformSpec("linux_i686")
        wheel = _wheel("foo-1.0-cp312-cp312-manylinux2014_i686.whl")
        assert _compatible(wheel, python_version="3.12", spec=spec)


class TestLinuxArmv7l:
    """``linux_armv7l`` emits the armv7l manylinux family from glibc 2.31 down."""

    def test_default_target_emits_manylinux_armv7l(self) -> None:
        platforms = _platform_tags_for_spec(PlatformSpec("linux_armv7l"))
        assert platforms[0] == "linux_armv7l"
        assert "manylinux_2_31_armv7l" in platforms
        assert "manylinux_2_17_armv7l" in platforms
        assert "manylinux2014_armv7l" in platforms
        assert "manylinux_2_5_armv7l" not in platforms
        assert not any(p.startswith("musllinux") for p in platforms)

    def test_accepts_a_manylinux_2_31_armv7l_wheel(self) -> None:
        spec = PlatformSpec("linux_armv7l")
        wheel = _wheel("foo-1.0-cp312-cp312-manylinux_2_31_armv7l.whl")
        assert _compatible(wheel, python_version="3.12", spec=spec)

    def test_accepts_a_manylinux_2_34_armv7l_wheel(self) -> None:
        """With no runs-on-libc, an above-2.31 armv7l wheel is installable."""
        spec = PlatformSpec("linux_armv7l")
        wheel = _wheel("foo-1.0-cp312-cp312-manylinux_2_34_armv7l.whl")
        assert _compatible(wheel, python_version="3.12", spec=spec)


class TestTagsForTarget:
    """``TagSet.for_spec`` produces the full PEP 425 tag set."""

    def test_python_315_emits_cp315_tags(self) -> None:
        """A 3.15 target builds cp315 interpreter tags."""
        spec = PlatformSpec("linux_x86_64")
        members = TagSet.for_spec(python_version="3.15", spec=spec).members
        assert any(t.interpreter == "cp315" for t in members)

    def test_python_315_free_threaded_emits_cp315t_abi(self) -> None:
        """A free-threaded 3.15 target builds the cp315t ABI tag."""
        spec = PlatformSpec("linux_x86_64", free_threaded=True)
        members = TagSet.for_spec(python_version="3.15", spec=spec).members
        assert any(t.abi == "cp315t" for t in members)

    def test_linux_includes_manylinux_at_or_below_runs_on_libc(self) -> None:
        """A glibc 2.17 target admits manylinux_2_17 and below."""
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 17))
        tag_strs = {
            str(t) for t in TagSet.for_spec(python_version="3.11", spec=spec).members
        }
        assert "cp311-cp311-manylinux_2_17_x86_64" in tag_strs
        assert "cp311-cp311-manylinux_2_5_x86_64" in tag_strs

    def test_linux_excludes_manylinux_above_runs_on_libc(self) -> None:
        """manylinux_2_28 needs glibc 2.28; a 2.17 target cannot run it."""
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 17))
        tag_strs = {
            str(t) for t in TagSet.for_spec(python_version="3.11", spec=spec).members
        }
        assert "cp311-cp311-manylinux_2_28_x86_64" not in tag_strs

    def test_default_linux_admits_any_manylinux(self) -> None:
        """With no runs-on-libc, an undeclared target admits old and new glibc."""
        spec = PlatformSpec("linux_x86_64")
        tag_strs = {
            str(t) for t in TagSet.for_spec(python_version="3.11", spec=spec).members
        }
        assert "cp311-cp311-manylinux_2_28_x86_64" in tag_strs
        assert "cp311-cp311-manylinux_2_39_x86_64" in tag_strs

    def test_linux_includes_legacy_aliases(self) -> None:
        """manylinux1, manylinux2010, manylinux2014 aliases are included."""
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 17))
        tag_strs = {
            str(t) for t in TagSet.for_spec(python_version="3.11", spec=spec).members
        }
        assert "cp311-cp311-manylinux1_x86_64" in tag_strs
        assert "cp311-cp311-manylinux2010_x86_64" in tag_strs
        assert "cp311-cp311-manylinux2014_x86_64" in tag_strs

    def test_linux_excludes_aliases_above_runs_on_libc(self) -> None:
        """manylinux2014 means glibc 2.17; a 2.12 target excludes it."""
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 12))
        tag_strs = {
            str(t) for t in TagSet.for_spec(python_version="3.11", spec=spec).members
        }
        assert "cp311-cp311-manylinux2014_x86_64" not in tag_strs
        assert "cp311-cp311-manylinux2010_x86_64" in tag_strs

    def test_aarch64_manylinux_stops_at_2_17(self) -> None:
        """aarch64 does not descend below glibc 2.17 (PEP 599)."""
        spec = PlatformSpec("linux_aarch64", runs_on_libc=(2, 17))
        tag_strs = {
            str(t) for t in TagSet.for_spec(python_version="3.11", spec=spec).members
        }
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
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 17))
        tag_strs = {
            str(t) for t in TagSet.for_spec(python_version="3.11", spec=spec).members
        }
        assert "cp311-cp311-manylinux1_x86_64" in tag_strs
        assert "cp311-cp311-manylinux_2_5_x86_64" in tag_strs

    def test_musl_includes_musllinux_at_or_below_version(self) -> None:
        """A musl 1.2 target admits musllinux_1_2 and below."""
        spec = PlatformSpec("linux_x86_64", libc="musl", runs_on_libc=(1, 2))
        tag_strs = {
            str(t) for t in TagSet.for_spec(python_version="3.11", spec=spec).members
        }
        assert "cp311-cp311-musllinux_1_2_x86_64" in tag_strs
        assert "cp311-cp311-musllinux_1_0_x86_64" in tag_strs

    def test_macos_arm64_default_accepts_any_wheel(self) -> None:
        """An undeclared ``macos_arm64`` sets no runs-on-macos: old and new admit."""
        spec = PlatformSpec("macos_arm64")
        tag_strs = {
            str(t) for t in TagSet.for_spec(python_version="3.11", spec=spec).members
        }
        assert "cp311-cp311-macosx_11_0_arm64" in tag_strs
        assert "cp311-cp311-macosx_12_0_arm64" in tag_strs
        assert "cp311-cp311-macosx_13_0_arm64" in tag_strs
        assert "cp311-cp311-macosx_26_0_arm64" in tag_strs

    def test_macos_x86_64_default_accepts_any_wheel(self) -> None:
        """An undeclared ``macos_x86_64`` admits both x86_64-era and newer wheels."""
        spec = PlatformSpec("macos_x86_64")
        tag_strs = {
            str(t) for t in TagSet.for_spec(python_version="3.11", spec=spec).members
        }
        assert "cp311-cp311-macosx_10_13_x86_64" in tag_strs
        assert "cp311-cp311-macosx_15_0_x86_64" in tag_strs

    def test_macos_explicit_runs_on_drops_newer_wheels(self) -> None:
        """A set ``runs_on_macos`` admits its version and older, and drops newer."""
        spec = PlatformSpec("macos_arm64", runs_on_macos=(14, 0))
        tag_strs = {
            str(t) for t in TagSet.for_spec(python_version="3.11", spec=spec).members
        }
        assert "cp311-cp311-macosx_14_0_arm64" in tag_strs
        assert "cp311-cp311-macosx_15_0_arm64" not in tag_strs

    def test_windows_amd64(self) -> None:
        """Windows amd64 generates ``win_amd64`` tags."""
        spec = PlatformSpec("windows_amd64")
        tag_strs = {
            str(t) for t in TagSet.for_spec(python_version="3.11", spec=spec).members
        }
        assert "cp311-cp311-win_amd64" in tag_strs

    def test_includes_universal_tags(self) -> None:
        """``py3-none-any`` and ``cp311-none-any`` are always in the set."""
        spec = PlatformSpec("linux_x86_64")
        tag_strs = {
            str(t) for t in TagSet.for_spec(python_version="3.11", spec=spec).members
        }
        assert "py3-none-any" in tag_strs
        assert "cp311-none-any" in tag_strs


class TestWheelCompatibility:
    """A wheel is a candidate iff its tags meet the target's tag set."""

    def test_an_oversized_compressed_tag_set_drops_the_wheel(self) -> None:
        """A PEP 425 compressed tag set is a cross product, and the index names it.

        A filename listing 40 interpreters, 40 abis and 40 platforms parses
        into 64000 tags.  The parser is bounded, and a filename over the bound
        is unreadable, which drops the whole candidate.
        """
        _parse_tag_str.cache_clear()
        try:
            big = ".".join(f"cp3{i}" for i in range(40))
            abis = ".".join(f"abi{i}" for i in range(40))
            plats = ".".join(f"plat{i}" for i in range(40))
            assert wheel_tag_set(f"pkg-1.0-{big}-{abis}-{plats}.whl") is None
        finally:
            _parse_tag_str.cache_clear()

    def test_repeated_filename_is_decomposed_once(self) -> None:
        """A repeated wheel filename is parsed once, not once per lookup."""
        filename = "starlette-1.3.1-py3-none-any.whl"
        if hasattr(wheel_tag_set, "cache_clear"):
            wheel_tag_set.cache_clear()

        with patch("nab_provider.tags._parse_tag_str", wraps=_parse_tag_str) as parse:
            first = wheel_tag_set(filename)
            repeats = [wheel_tag_set(filename) for _ in range(8)]

        assert first == _parse_tag_str("py3-none-any")
        assert all(result is first for result in repeats)
        assert parse.call_count == 1

    def test_accepts_manylinux_at_runs_on_libc(self) -> None:
        """A manylinux_2_17 wheel matches a glibc 2.17 target."""
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 17))
        wheel = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        assert _compatible(wheel, python_version="3.11", spec=spec)

    def test_rejects_manylinux_above_runs_on_libc(self) -> None:
        """A manylinux_2_28 wheel does not match a glibc 2.17 target."""
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 17))
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

    def test_four_segment_filename_is_not_a_wheel(self) -> None:
        """Four segments is one short, even when the last three parse as tags."""
        assert wheel_tag_set("foo-py3-none-any.whl") is None
        assert wheel_tag_set("foo-1.0-py3-none.whl") is None

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
                "nab_provider.tags.ptags.parse_tag",
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
        assert TagSet.for_spec(python_version="3.11", spec=spec).pick(wheels) is None

    def test_default_macos_arm64_selects_modern_only_wheel(self) -> None:
        """A version shipping only a ``macosx_12_0`` arm64 wheel is selectable."""
        spec = PlatformSpec("macos_arm64")
        wheels = [_wheel("pkg-2.2.0-cp312-cp312-macosx_12_0_arm64.whl")]
        chosen = TagSet.for_spec(python_version="3.12", spec=spec).pick(wheels)
        assert chosen is not None
        assert chosen.filename == "pkg-2.2.0-cp312-cp312-macosx_12_0_arm64.whl"

    def test_default_macos_arm64_prefers_native_over_universal(self) -> None:
        """A ``macosx_12_0_arm64`` wheel beats the ``py3-none-any`` fallback."""
        spec = PlatformSpec("macos_arm64")
        wheels = [
            _wheel("pkg-2.2.0-py3-none-any.whl"),
            _wheel("pkg-2.2.0-cp312-cp312-macosx_12_0_arm64.whl"),
        ]
        chosen = TagSet.for_spec(python_version="3.12", spec=spec).pick(wheels)
        assert chosen is not None
        assert chosen.filename == "pkg-2.2.0-cp312-cp312-macosx_12_0_arm64.whl"

    def test_specific_beats_universal(self) -> None:
        """A platform-specific wheel beats py3-none-any."""
        spec = PlatformSpec("linux_x86_64")
        wheels = [
            _wheel("pkg-1.0-py3-none-any.whl"),
            _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl"),
        ]
        chosen = TagSet.for_spec(python_version="3.11", spec=spec).pick(wheels)
        assert chosen is not None
        assert "manylinux" in chosen.filename

    def test_universal_fallback(self) -> None:
        """When only py3-none-any is compatible, it wins."""
        spec = PlatformSpec("linux_x86_64")
        wheels = [_wheel("pkg-1.0-py3-none-any.whl")]
        chosen = TagSet.for_spec(python_version="3.11", spec=spec).pick(wheels)
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
        chosen = TagSet.for_spec(python_version="3.11", spec=spec).pick(wheels)
        assert chosen is good

    def test_higher_glibc_wheel_wins_over_lower(self) -> None:
        """Among manylinux candidates, the highest runnable glibc wins.

        On a glibc 2.17 target both manylinux_2_5 and manylinux_2_17 are
        compatible.  manylinux_2_17 is the more-specific tag (PEP 600
        recommends preferring it) and should be selected.
        """
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 17))
        wheels = [
            _wheel("pkg-1.0-cp311-cp311-manylinux_2_5_x86_64.whl"),
            _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl"),
        ]
        chosen = TagSet.for_spec(python_version="3.11", spec=spec).pick(wheels)
        assert chosen is not None
        assert "manylinux_2_17" in chosen.filename

    def test_legacy_alias_ranks_with_its_glibc(self) -> None:
        """A legacy alias ranks at its glibc, not after all PEP 600 tags.

        manylinux2014 means glibc 2.17, so it must beat a glibc-2.5
        wheel.  packaging.tags interleaves each legacy alias right after
        its equivalent manylinux_X_Y tag, so manylinux2014 outranks
        manylinux_2_5.
        """
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 17))
        wheels = [
            _wheel("pkg-1.0-cp311-cp311-manylinux_2_5_x86_64.whl"),
            _wheel("pkg-1.0-cp311-cp311-manylinux2014_x86_64.whl"),
        ]
        chosen = TagSet.for_spec(python_version="3.11", spec=spec).pick(wheels)
        assert chosen is not None
        assert "manylinux2014" in chosen.filename

    def test_skips_worse_candidate_after_best_set(self) -> None:
        """A later, worse-ranked wheel does not displace an earlier best.

        Reverse of the above: when manylinux_2_17 is iterated FIRST
        and manylinux_2_5 second, the loop must keep the first as
        best and not replace it.
        """
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 17))
        wheels = [
            _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl"),
            _wheel("pkg-1.0-cp311-cp311-manylinux_2_5_x86_64.whl"),
        ]
        chosen = TagSet.for_spec(python_version="3.11", spec=spec).pick(wheels)
        assert chosen is not None
        assert "manylinux_2_17" in chosen.filename

    def test_higher_build_tag_wins_at_same_rank(self) -> None:
        """Among same-tag wheels, the higher PEP 427 build tag wins."""
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 17))
        build1 = _wheel("pkg-1.0-1-cp311-cp311-manylinux_2_17_x86_64.whl")
        build5 = _wheel("pkg-1.0-5-cp311-cp311-manylinux_2_17_x86_64.whl")
        chosen = TagSet.for_spec(python_version="3.11", spec=spec).pick(
            [build1, build5]
        )
        assert chosen is build5

    def test_build_tag_selection_is_order_independent(self) -> None:
        """The same wheel is chosen regardless of index file order."""
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 17))
        build1 = _wheel("pkg-1.0-1-cp311-cp311-manylinux_2_17_x86_64.whl")
        build5 = _wheel("pkg-1.0-5-cp311-cp311-manylinux_2_17_x86_64.whl")
        forward = TagSet.for_spec(python_version="3.11", spec=spec).pick(
            [build1, build5]
        )
        reverse = TagSet.for_spec(python_version="3.11", spec=spec).pick(
            [build5, build1]
        )
        assert forward is build5
        assert reverse is build5

    def test_highest_build_tag_wins_among_three_at_one_rank(self) -> None:
        """A third wheel at the same rank is weighed against the running best."""
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 17))
        build1 = _wheel("pkg-1.0-1-cp311-cp311-manylinux_2_17_x86_64.whl")
        build5 = _wheel("pkg-1.0-5-cp311-cp311-manylinux_2_17_x86_64.whl")
        build3 = _wheel("pkg-1.0-3-cp311-cp311-manylinux_2_17_x86_64.whl")
        chosen = TagSet.for_spec(python_version="3.11", spec=spec).pick(
            [build1, build5, build3]
        )
        assert chosen is build5

    def test_better_rank_discards_the_incumbent_build_key(self) -> None:
        """A better tag rank drops the incumbent's build key instead of carrying it.

        The first two wheels tie at manylinux_2_5 and settle on build 9.
        manylinux_2_17 then outranks both, so build 5 is weighed against
        build 0 rather than against the discarded build 9.
        """
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 17))
        generic9 = _wheel("pkg-1.0-9-cp311-cp311-manylinux_2_5_x86_64.whl")
        generic1 = _wheel("pkg-1.0-1-cp311-cp311-manylinux_2_5_x86_64.whl")
        specific0 = _wheel("pkg-1.0-0-cp311-cp311-manylinux_2_17_x86_64.whl")
        specific5 = _wheel("pkg-1.0-5-cp311-cp311-manylinux_2_17_x86_64.whl")
        chosen = TagSet.for_spec(python_version="3.11", spec=spec).pick(
            [generic9, generic1, specific0, specific5]
        )
        assert chosen is specific5

    def test_build_tagged_wheel_beats_untagged_at_same_rank(self) -> None:
        """An absent build tag sorts lowest, so a tagged wheel wins."""
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 17))
        untagged = _wheel("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        build3 = _wheel("pkg-1.0-3-cp311-cp311-manylinux_2_17_x86_64.whl")
        chosen = TagSet.for_spec(python_version="3.11", spec=spec).pick(
            [untagged, build3]
        )
        assert chosen is build3

    def test_timestamp_build_tag_beats_untagged(self) -> None:
        """A rebuild numbered with a timestamp sorts above an absent tag."""
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 28))
        untagged = _wheel("pkg-1.0-py3-none-any.whl")
        rebuild = _wheel("pkg-1.0-1753900000-py3-none-any.whl")
        chosen = TagSet.for_spec(python_version="3.12", spec=spec).pick(
            [untagged, rebuild]
        )
        assert chosen is rebuild

    def test_higher_timestamp_build_tag_wins_either_order(self) -> None:
        """Two timestamp rebuilds order by build number, not by listing order."""
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 28))
        older = _wheel("pkg-1.0-1753900000-py3-none-any.whl")
        newer = _wheel("pkg-1.0-1753900001-py3-none-any.whl")
        tags = TagSet.for_spec(python_version="3.12", spec=spec)
        assert tags.pick([older, newer]) is newer
        assert tags.pick([newer, older]) is newer

    def test_an_absurd_build_number_is_malformed(self) -> None:
        """The index names the build tag, and int() raises above 4300 digits."""
        spec = PlatformSpec("linux_x86_64")
        build5 = _wheel("pkg-1.0-5-cp311-cp311-manylinux_2_17_x86_64.whl")
        absurd = _wheel(f"pkg-1.0-{'9' * 5000}-cp311-cp311-manylinux_2_17_x86_64.whl")
        chosen = TagSet.for_spec(python_version="3.11", spec=spec).pick(
            [absurd, build5]
        )
        assert chosen is build5

    def test_malformed_build_tag_treated_as_absent(self) -> None:
        """A build segment without a leading digit sorts lowest."""
        spec = PlatformSpec("linux_x86_64", runs_on_libc=(2, 17))
        malformed = _wheel("pkg-1.0-x-cp311-cp311-manylinux_2_17_x86_64.whl")
        build5 = _wheel("pkg-1.0-5-cp311-cp311-manylinux_2_17_x86_64.whl")
        chosen = TagSet.for_spec(python_version="3.11", spec=spec).pick(
            [malformed, build5]
        )
        assert chosen is build5


class TestWheelRank:
    """wheel_rank exposes pick's ordering as a comparable key.

    Two wheels the target's own rules cannot order return an equal,
    non-None key; a more-specific wheel returns a strictly lower key; a
    rejected or non-wheel filename returns None.
    """

    def test_tie_wheels_share_rank(self) -> None:
        """py2.py3-none-any and py3-none-any tie for a py3 target."""
        tags = TagSet.for_spec(python_version="3.11", spec=PlatformSpec("linux_x86_64"))
        both = tags.wheel_rank("pkg-1.0-py2.py3-none-any.whl")
        py3 = tags.wheel_rank("pkg-1.0-py3-none-any.whl")
        assert both is not None
        assert both == py3

    def test_specific_outranks_generic(self) -> None:
        """A manylinux wheel ranks strictly below the py3-none-any fallback."""
        tags = TagSet.for_spec(python_version="3.11", spec=PlatformSpec("linux_x86_64"))
        specific = tags.wheel_rank("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        generic = tags.wheel_rank("pkg-1.0-py3-none-any.whl")
        assert specific is not None
        assert generic is not None
        assert specific < generic

    def test_rejected_wheel_has_no_rank(self) -> None:
        """A wheel the target cannot install ranks None."""
        tags = TagSet.for_spec(python_version="3.11", spec=PlatformSpec("linux_x86_64"))
        assert tags.wheel_rank("pkg-1.0-cp311-cp311-win_amd64.whl") is None

    def test_non_wheel_has_no_rank(self) -> None:
        """An unparseable filename ranks None."""
        tags = TagSet.for_spec(python_version="3.11", spec=PlatformSpec("linux_x86_64"))
        assert tags.wheel_rank("garbage.whl") is None

    def test_rank_carries_build_key(self) -> None:
        """Same-tag wheels order by build key, higher build sorting later."""
        tags = TagSet.for_spec(python_version="3.11", spec=PlatformSpec("linux_x86_64"))
        untagged = tags.wheel_rank("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
        build5 = tags.wheel_rank("pkg-1.0-5-cp311-cp311-manylinux_2_17_x86_64.whl")
        assert untagged is not None
        assert build5 is not None
        assert untagged[0] == build5[0]
        assert untagged[1] < build5[1]

    def test_timestamp_build_tags_do_not_tie(self) -> None:
        """Timestamp-numbered rebuilds rank apart, so they never tie."""
        tags = TagSet.for_spec(python_version="3.12", spec=PlatformSpec("linux_x86_64"))
        first = tags.wheel_rank("pkg-1.0-20260730123456-py3-none-any.whl")
        second = tags.wheel_rank("pkg-1.0-20260730123457-py3-none-any.whl")
        assert first is not None
        assert second is not None
        assert first < second


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
        chosen = TagSet.for_spec(
            python_version="3.11", spec=self.SPEC, implementation="pypy"
        ).pick([pure, native])
        assert chosen is native

    def test_pypy_pure_python_pp3_wheel_matches(self) -> None:
        """A ``pp3-none-any`` PyPy pure-Python wheel matches its target."""
        wheel = _wheel("foo-1.0-pp3-none-any.whl")
        assert _compatible(
            wheel, python_version="3.10", spec=self.SPEC, implementation="pypy"
        )

    def test_pypy_ppxy_none_any_wheel_rejected(self) -> None:
        """``ppXY-none-any`` is not a tag any PyPy install advertises."""
        wheel = _wheel("bar-2.0-pp310-none-any.whl")
        assert not _compatible(
            wheel, python_version="3.10", spec=self.SPEC, implementation="pypy"
        )

    def test_pypy_compat_set_interpreter_any_tag_is_major_only(self) -> None:
        """The interpreter-specific ``any`` tag is ``pp3-none-any``, not ``ppXY``."""
        compat = TagSet.for_spec(
            python_version="3.10", spec=self.SPEC, implementation="pypy"
        ).members
        assert Tag("pp3", "none", "any") in compat
        assert Tag("pp310", "none", "any") not in compat

    def test_compat_tag_sets_differ_by_implementation(self) -> None:
        """The CPython and PyPy tuples accept disjoint native-tag sets."""
        cp = TagSet.for_spec(python_version="3.11", spec=self.SPEC).members
        pp = TagSet.for_spec(
            python_version="3.11", spec=self.SPEC, implementation="pypy"
        ).members
        cp_native = {t for t in cp if t.abi.startswith("cp")}
        pp_native = {t for t in pp if t.abi.startswith("pypy")}
        assert cp_native
        assert pp_native
        assert cp_native.isdisjoint(pp)
        assert pp_native.isdisjoint(cp)


class TestTagSetHostConstructors:
    """The host constructors take their tags from an injected source."""

    _HOST = (
        Tag("cp313", "cp313", "manylinux_2_39_x86_64"),
        Tag("cp313", "cp313", "linux_x86_64"),
        Tag("py3", "none", "any"),
    )

    def _host_tags(self) -> tuple[Tag, ...]:
        return self._HOST

    def test_for_host_takes_the_source_verbatim(self) -> None:
        """packaging already answers this for the live machine."""
        tags = TagSet.for_host(tags_source=self._host_tags)
        assert tags.ordered == self._HOST
        assert tags.members == frozenset(self._HOST)

    def test_for_host_defaults_to_sys_tags(self) -> None:
        """Without a source, the running interpreter's tags are used."""
        assert TagSet.for_host().ordered

    def test_for_host_python_keeps_the_host_platform_order(self) -> None:
        """The platform axis carries over, in the host's own preference order."""
        tags = TagSet.for_host_python("3.11", tags_source=self._host_tags)
        platforms = [t.platform for t in tags.ordered if t.interpreter == "cp311"]
        assert platforms[:2] == ["manylinux_2_39_x86_64", "linux_x86_64"]

    def test_for_host_python_drops_the_any_platform(self) -> None:
        """``any`` is not a machine, so it never seeds the platform axis."""
        tags = TagSet.for_host_python("3.11", tags_source=self._host_tags)
        assert Tag("cp311", "cp311", "any") not in tags.members
        assert Tag("py3", "none", "any") in tags.members

    def test_for_host_python_needs_a_non_empty_source(self) -> None:
        with pytest.raises(ValueError, match="no tags"):
            TagSet.for_host_python("3.11", tags_source=tuple)

    def _free_threaded_host(self) -> tuple[Tag, ...]:
        return (
            Tag("cp314", "cp314t", "manylinux_2_39_x86_64"),
            Tag("py3", "none", "any"),
        )

    def test_an_unknown_host_interpreter_raises(self) -> None:
        """Guessing CPython would hand the host a tag set it can load none of."""

        def graalpy_tags() -> tuple[Tag, ...]:
            return (
                Tag("graalpy311", "graalpy311_native", "manylinux_2_39_x86_64"),
                Tag("py3", "none", "any"),
            )

        with pytest.raises(ValueError, match="unsupported interpreter"):
            TagSet.for_host_python("3.11", tags_source=graalpy_tags)

    def test_free_threaded_host_does_not_carry_to_an_older_python(self) -> None:
        """``cp310t`` never existed, so a target naming it matches no wheel."""
        tags = TagSet.for_host_python("3.10", tags_source=self._free_threaded_host)
        assert {t.abi for t in tags.ordered if t.interpreter == "cp310"} == {
            "cp310",
            "abi3",
            "none",
        }

    def test_free_threaded_host_carries_to_a_python_that_has_it(self) -> None:
        tags = TagSet.for_host_python("3.13", tags_source=self._free_threaded_host)
        assert "cp313t" in {t.abi for t in tags.ordered}


class TestTagSetAccepts:
    """``accepts`` answers wheel compatibility from the filename alone."""

    _TAGS = TagSet.for_spec(python_version="3.11", spec=PlatformSpec("linux_x86_64"))

    def test_compatible_wheel_accepted(self) -> None:
        assert self._TAGS.accepts("pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")

    def test_incompatible_wheel_rejected(self) -> None:
        assert not self._TAGS.accepts("pkg-1.0-cp311-cp311-win_amd64.whl")

    def test_unparseable_filename_rejected(self) -> None:
        assert not self._TAGS.accepts("pkg-1.0.tar.gz")

    def test_rank_orders_specific_before_generic(self) -> None:
        """The rank drives the install pick: lowest index wins."""
        specific = Tag("cp311", "cp311", "manylinux_2_28_x86_64")
        generic = Tag("py3", "none", "any")
        assert self._TAGS.rank[specific] < self._TAGS.rank[generic]


class TestPythonAxisAccepts:
    """``python_axis_accepts`` answers the Python axis alone, any platform.

    It is what a target whose tags a marker overlay disowned can still ask:
    the overlay cannot rebuild the platform tags, but the Python version and
    the implementation survive it.
    """

    def test_the_targets_own_interpreter_is_accepted(self) -> None:
        assert python_axis_accepts(
            "3.7", "cpython", "pkg-1.0-cp37-cp37m-manylinux1_x86_64.whl"
        )

    def test_a_foreign_platform_is_still_accepted(self) -> None:
        """The platform axis is projected out, so it decides nothing."""
        assert python_axis_accepts("3.7", "cpython", "pkg-1.0-cp37-cp37m-win_amd64.whl")

    def test_another_interpreter_is_rejected(self) -> None:
        assert not python_axis_accepts(
            "3.7", "cpython", "pkg-1.0-cp27-cp27m-manylinux1_x86_64.whl"
        )
        assert not python_axis_accepts(
            "3.7", "cpython", "pkg-1.0-cp35-cp35m-manylinux1_x86_64.whl"
        )

    def test_an_older_abi3_wheel_is_accepted(self) -> None:
        """PEP 384's stable ABI runs forward, so an older abi3 wheel installs."""
        assert python_axis_accepts(
            "3.7", "cpython", "pkg-1.0-cp35-abi3-manylinux1_x86_64.whl"
        )

    def test_a_newer_abi3_wheel_is_rejected(self) -> None:
        assert not python_axis_accepts(
            "3.7", "cpython", "pkg-1.0-cp38-abi3-manylinux1_x86_64.whl"
        )

    def test_generic_wheels_track_the_major(self) -> None:
        assert python_axis_accepts("3.7", "cpython", "pkg-1.0-py2.py3-none-any.whl")
        assert python_axis_accepts("3.7", "cpython", "pkg-1.0-py3-none-any.whl")
        assert not python_axis_accepts("3.7", "cpython", "pkg-1.0-py2-none-any.whl")

    def test_the_implementation_separates_the_axes(self) -> None:
        """A PyPy target loads no CPython ABI, and the reverse."""
        pypy = "pkg-1.0-pp310-pypy310_pp73-manylinux_2_17_x86_64.whl"
        cpython = "pkg-1.0-cp310-cp310-manylinux_2_17_x86_64.whl"
        assert python_axis_accepts("3.10", "pypy", pypy)
        assert not python_axis_accepts("3.10", "cpython", pypy)
        assert not python_axis_accepts("3.10", "pypy", cpython)

    def test_both_free_threaded_builds_are_accepted(self) -> None:
        """A marker environment does not name the build, so neither is excluded."""
        assert python_axis_accepts(
            "3.13", "cpython", "pkg-1.0-cp313-cp313-manylinux_2_17_x86_64.whl"
        )
        assert python_axis_accepts(
            "3.13", "cpython", "pkg-1.0-cp313-cp313t-manylinux_2_17_x86_64.whl"
        )

    def test_an_unparseable_filename_is_accepted(self) -> None:
        """It carries no tags to reject it by."""
        assert python_axis_accepts("3.7", "cpython", "pkg-1.0.tar.gz")

    def test_an_implementation_without_tag_rules_has_no_opinion(self) -> None:
        """nab has no tag rules for it, and guessing CPython's would invert them."""
        assert python_axis_accepts(
            "3.10",
            "graalpy",
            "pkg-1.0-graalpy310-graalpy240_310_native-manylinux_2_17_x86_64.whl",
        )
        assert python_axis_accepts(
            "3.10", "graalpy", "pkg-1.0-cp310-cp310-manylinux_2_17_x86_64.whl"
        )


class TestLinuxPlatformOrderMatchesSysTags:
    """A declared linux target ranks platform tags the way ``sys_tags`` does.

    ``packaging.tags`` yields the plain ``linux_<arch>`` tag before any
    manylinux one, so an installer on a real machine prefers a plain
    ``linux_x86_64`` wheel.  A declared target that ranked manylinux first
    would predict a wheel the target does not install.  Only an index that
    serves plain ``linux_*`` wheels can tell the two apart; PyPI rejects
    them, so this is about local and private indexes.
    """

    def test_plain_linux_tag_comes_first(self) -> None:
        platforms = _platform_tags_for_spec(PlatformSpec("linux_x86_64"))
        assert platforms[0] == "linux_x86_64"
        # No runs-on-libc enumerates from the max down, so the top is manylinux_2_99.
        assert platforms[1] == "manylinux_2_99_x86_64"

    def test_musl_target_ranks_the_plain_tag_first_too(self) -> None:
        platforms = _platform_tags_for_spec(PlatformSpec("linux_x86_64", libc="musl"))
        assert platforms[0] == "linux_x86_64"
        assert platforms[1] == "musllinux_1_99_x86_64"

    def test_plain_linux_wheel_wins_over_manylinux(self) -> None:
        """The install pick follows the ranking, as pip's does."""
        tags = TagSet.for_spec(python_version="3.11", spec=PlatformSpec("linux_x86_64"))
        wheels = [
            _wheel("pkg-1.0-cp311-cp311-manylinux_2_28_x86_64.whl"),
            _wheel("pkg-1.0-cp311-cp311-linux_x86_64.whl"),
        ]
        chosen = tags.pick(wheels)
        assert chosen is not None
        assert chosen.filename == "pkg-1.0-cp311-cp311-linux_x86_64.whl"

    def test_the_host_ranks_it_the_same_way(self) -> None:
        """The host path is ``sys_tags`` verbatim, so the two paths agree."""
        host = TagSet.for_host(
            tags_source=lambda: (
                Tag("cp311", "cp311", "linux_x86_64"),
                Tag("cp311", "cp311", "manylinux_2_28_x86_64"),
            )
        )
        declared = TagSet.for_spec(
            python_version="3.11", spec=PlatformSpec("linux_x86_64")
        )
        plain = Tag("cp311", "cp311", "linux_x86_64")
        many = Tag("cp311", "cp311", "manylinux_2_28_x86_64")
        assert host.rank[plain] < host.rank[many]
        assert declared.rank[plain] < declared.rank[many]


# The platform axes production expands: one per declared platform id, the bare
# ``any`` axis ``_python_axis_tags`` projects over, and the empty list
# ``TagSet.for_host_python`` builds when the host advertises no platform but
# ``any``.
_ORDER_PLATFORM_LISTS = [
    *(
        pytest.param(_platform_tags_for_spec(PlatformSpec(platform_id)), id=platform_id)
        for platform_id in sorted(_PLATFORM_KIND)
    ),
    pytest.param(["any"], id="any-axis"),
    pytest.param([], id="no-platforms"),
]


class TestSharedTagInstances:
    """A target's platform axis is expanded from a template, over shared tags.

    ``_tags_in_order`` derives the order once per (python, implementation,
    build) against a placeholder platform, then expands each placeholder entry
    over the real platform list.  That reproduces packaging's sequence only
    while packaging pairs an (interpreter, abi) with every platform before
    moving to the next, so the expansion is pinned against packaging's own
    output: a reordering would silently change which wheel a target picks.
    """

    @pytest.mark.parametrize("platforms", _ORDER_PLATFORM_LISTS)
    @pytest.mark.parametrize("python_version", ["3.10", "3.13"])
    @pytest.mark.parametrize("implementation", ["cpython", "pypy"])
    def test_order_matches_packaging(
        self, platforms: list[str], python_version: str, implementation: str
    ) -> None:
        assert tuple(
            _tags_in_order(
                python_version, platforms, implementation, free_threaded=False
            )
        ) == tuple(
            _packaging_tags(
                python_version, platforms, implementation, free_threaded=False
            )
        )

    def test_order_matches_packaging_free_threaded(self) -> None:
        platforms = _platform_tags_for_spec(PlatformSpec("linux_x86_64"))
        assert tuple(
            _tags_in_order("3.13", platforms, "cpython", free_threaded=True)
        ) == tuple(_packaging_tags("3.13", platforms, "cpython", free_threaded=True))

    def test_two_pythons_reuse_one_instance_per_tag(self) -> None:
        spec = PlatformSpec("linux_x86_64")
        older = TagSet.for_spec(python_version="3.12", spec=spec).ordered
        newer = TagSet.for_spec(python_version="3.13", spec=spec).ordered

        shared = set(older) & set(newer)
        assert shared

        instances = {id(tag) for tag in older}
        assert all(id(tag) in instances for tag in newer if tag in shared)


class TestWheelRankAgreesWithExpandedOrder:
    """``wheel_rank`` places a wheel where the expanded order puts its tag.

    ``rank`` reads the index off :attr:`TagSet.ordered`; ``wheel_rank`` adds
    a platform's offset to its interpreter/abi block instead, and never
    expands the product.  Two answers to one question, so every tag a target
    accepts is asked both ways: an offset out by one would tie the last
    platform of a block with the entry after it, and ``pick`` would then
    break that pair on the build tag rather than on specificity.
    """

    @pytest.mark.parametrize(
        "spec",
        [
            pytest.param(PlatformSpec("windows_amd64"), id="one-platform"),
            pytest.param(PlatformSpec("linux_x86_64"), id="many-platforms"),
        ],
    )
    def test_every_accepted_tag_places_the_same_both_ways(
        self, spec: PlatformSpec
    ) -> None:
        tags = TagSet.for_spec(python_version="3.13", spec=spec)

        keys = {tag: tags.wheel_rank(f"p-1.0-{tag}.whl") for tag in tags.ordered}
        assert None not in keys.values()

        placed = {tag: key[0] for tag, key in keys.items() if key is not None}
        assert placed == dict(tags.rank)
