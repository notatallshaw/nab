"""Compare declared target tags, compressed tag parsing and wheel selection with packaging."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from packaging import tags as upstream_tags
from packaging import utils as upstream_utils
from packaging.utils import BuildTag

from nab_provider.records import WheelFile
from nab_provider.tags import (
    _MACOS_TAG_FLOOR,
    _PLATFORM_ARCH,
    PlatformSpec,
    TagSet,
    _platform_tags_for_spec,
)
from nab_provider.value import replace

from .settings import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

PY_VERSIONS = ("3.8", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14")
PLATFORM_IDS = (
    "linux_x86_64",
    "linux_aarch64",
    "macos_arm64",
    "macos_x86_64",
    "windows_amd64",
)

LINUX_IDS = tuple(p for p in PLATFORM_IDS if p.startswith("linux_"))
MACOS_IDS = tuple(p for p in PLATFORM_IDS if p.startswith("macos_"))

# A knob only its own platform reads is a construction error, so each
# platform draws its own.
linux_specs = st.one_of(
    st.builds(
        PlatformSpec,
        platform_id=st.sampled_from(LINUX_IDS),
        libc=st.just("glibc"),
        runs_on_libc=st.tuples(st.just(2), st.integers(0, 35)),
        free_threaded=st.booleans(),
    ),
    st.builds(
        PlatformSpec,
        platform_id=st.sampled_from(LINUX_IDS),
        libc=st.just("musl"),
        runs_on_libc=st.tuples(st.just(1), st.integers(0, 4)),
        free_threaded=st.booleans(),
    ),
)
macos_specs = st.sampled_from(MACOS_IDS).flatmap(
    lambda platform_id: st.builds(
        PlatformSpec,
        platform_id=st.just(platform_id),
        runs_on_macos=st.none()
        | st.tuples(st.integers(10, 15), st.integers(0, 15)).filter(
            lambda v: v >= _MACOS_TAG_FLOOR[_PLATFORM_ARCH[platform_id]]
        ),
        free_threaded=st.booleans(),
    )
)
windows_specs = st.builds(
    PlatformSpec,
    platform_id=st.just("windows_amd64"),
    free_threaded=st.booleans(),
)
specs = st.one_of(linux_specs, macos_specs, windows_specs)


def _triples(tag_iter: object) -> list[tuple[str, str, str]]:
    """Flatten an iterable of ``Tag`` into (interpreter, abi, platform) triples."""
    return [(t.interpreter, t.abi, t.platform) for t in tag_iter]  # type: ignore[attr-defined]


def _oracle_tags_in_order(
    python_version: str,
    platforms: list[str],
    implementation: str,
    *,
    free_threaded: bool,
) -> list[tuple[str, str, str]]:
    """Rebuild a declared target's tag order with upstream ``packaging.tags``."""
    major, minor = (int(p) for p in python_version.split("."))
    py = (major, minor)
    out: list[tuple[str, str, str]] = []
    if implementation == "pypy":
        interpreter = f"pp{major}{minor}"
        abi = f"pypy{major}{minor}_pp73"
        out += [(interpreter, abi, p) for p in platforms]
        out += [(interpreter, "none", p) for p in platforms]
        # Upstream ``sys_tags`` hands ``compatible_tags`` the major-only
        # ``pp3`` on PyPy, so the interpreter-specific "any" tag a real
        # PyPy advertises is ``pp3-none-any``.
        compat_interpreter = f"pp{major}"
    else:
        interpreter = f"cp{major}{minor}"
        cp_abi = interpreter + ("t" if free_threaded else "")
        out += _triples(
            upstream_tags.cpython_tags(
                python_version=py, abis=[cp_abi], platforms=platforms
            )
        )
        compat_interpreter = interpreter
    out += _triples(
        upstream_tags.compatible_tags(
            python_version=py, interpreter=compat_interpreter, platforms=platforms
        )
    )
    return out


class TestTagOrderMatchesUpstream:
    """``TagSet.for_spec`` defines the install-preference ranking every
    wheel choice uses. Rebuilding it with upstream
    ``cpython_tags``/``compatible_tags`` over the same platform list
    must agree exactly.
    """

    @given(
        python_version=st.sampled_from(PY_VERSIONS),
        spec=specs,
        implementation=st.sampled_from(["cpython", "pypy"]),
    )
    @PROPERTY_SETTINGS
    def test_tag_order_matches_upstream(
        self, python_version: str, spec: PlatformSpec, implementation: str
    ) -> None:
        """Nab tag order equals the upstream-rebuilt order."""
        if implementation == "pypy":
            spec = replace(spec, pypy_abi="pp73")
        platforms = _platform_tags_for_spec(spec)
        got = _triples(
            TagSet.for_spec(
                python_version=python_version, spec=spec, implementation=implementation
            ).ordered
        )
        expected = _oracle_tags_in_order(
            python_version,
            platforms,
            implementation,
            free_threaded=spec.free_threaded,
        )
        assert got == expected


def _wheel(filename: str) -> WheelFile:
    """Build a minimal ``WheelFile`` for a literal filename."""
    return WheelFile(
        filename=filename,
        url=f"https://example.com/{filename}",
        version="1.0",
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )


KNOWN_PLATS = (
    "any",
    "manylinux_2_17_x86_64",
    "manylinux2014_x86_64",
    "manylinux_2_28_x86_64",
    "manylinux1_x86_64",
    "musllinux_1_2_x86_64",
    "linux_x86_64",
    "macosx_11_0_arm64",
    "macosx_10_13_x86_64",
    "macosx_10_9_universal2",
    "win_amd64",
    "manylinux_2_17_aarch64",
)
KNOWN_PYS = (
    "py3",
    "py2.py3",
    "cp38",
    "cp39",
    "cp310",
    "cp311",
    "cp312",
    "cp313",
    "pp310",
)
KNOWN_ABIS = ("none", "abi3", "cp310", "cp311", "cp312", "pypy310_pp73")


@st.composite
def realistic_wheels(draw: st.DrawFn) -> WheelFile:
    """Generate a wheel with realistic interpreter/abi/platform tags."""
    py = draw(st.sampled_from(KNOWN_PYS))
    abi = draw(st.sampled_from(KNOWN_ABIS))
    plat = draw(st.sampled_from(KNOWN_PLATS))
    return _wheel(f"pkg-1.0-{py}-{abi}-{plat}.whl")


class TestSelectWheelMinimizesUpstreamRank:
    """`PEP 425`_ preference: installers pick the wheel matching the
    earliest tag in the compatibility order.  The selected wheel's best
    rank, computed with upstream packaging throughout, must equal the
    minimum over all candidate wheels.

    .. _PEP 425: https://peps.python.org/pep-0425/#use
    """

    @given(
        wheels=st.lists(realistic_wheels(), min_size=0, max_size=8),
        python_version=st.sampled_from(PY_VERSIONS),
        spec=specs,
        implementation=st.sampled_from(["cpython", "pypy"]),
    )
    @PROPERTY_SETTINGS
    def test_select_wheel_minimizes_upstream_rank(
        self,
        wheels: list[WheelFile],
        python_version: str,
        spec: PlatformSpec,
        implementation: str,
    ) -> None:
        """The selected wheel's best upstream rank is the minimum over all wheels."""
        if implementation == "pypy":
            spec = replace(spec, pypy_abi="pp73")
        platforms = _platform_tags_for_spec(spec)
        order = _oracle_tags_in_order(
            python_version,
            platforms,
            implementation,
            free_threaded=spec.free_threaded,
        )
        rank = {t: i for i, t in enumerate(order)}

        def best_rank(w: WheelFile) -> int | None:
            stem = w.filename[:-4]
            tag_str = "-".join(stem.split("-")[-3:])
            triples = {
                (t.interpreter, t.abi, t.platform)
                for t in upstream_tags.parse_tag(tag_str)
            }
            ranks = [rank[t] for t in triples if t in rank]
            return min(ranks) if ranks else None

        oracle_ranks = [r for w in wheels if (r := best_rank(w)) is not None]
        chosen = TagSet.for_spec(
            python_version=python_version, spec=spec, implementation=implementation
        ).pick(wheels)
        if not oracle_ranks:
            assert chosen is None
            return
        assert chosen is not None
        assert best_rank(chosen) == min(oracle_ranks), (
            f"chose {chosen.filename} rank {best_rank(chosen)}; "
            f"best available {min(oracle_ranks)}"
        )


BUILD_TAGS = (
    "0",
    "1",
    "5",
    "42",
    "999999999",
    "1000000000",
    "1753900000",
    "1753900001",
    "20260730123456",
    "3post1",
)


class TestBuildTagOrderMatchesUpstream:
    """`PEP 427`_ orders same-tag wheels by build number, an absent tag
    lowest.  Upstream ``parse_wheel_filename`` reads the whole leading
    digit run, so ``TagSet.pick`` must choose a wheel whose upstream
    build tag is the maximum over all candidate wheels.

    .. _PEP 427: https://peps.python.org/pep-0427/#file-name-convention
    """

    @given(
        builds=st.lists(
            st.sampled_from(BUILD_TAGS), min_size=1, max_size=4, unique=True
        ),
        untagged=st.booleans(),
        python_version=st.sampled_from(PY_VERSIONS),
    )
    @PROPERTY_SETTINGS
    def test_pick_prefers_the_highest_upstream_build_tag(
        self, builds: list[str], untagged: bool, python_version: str
    ) -> None:
        """The picked wheel carries the highest upstream build tag."""
        wheels = [_wheel(f"pkg-1.0-{build}-py3-none-any.whl") for build in builds]
        if untagged:
            wheels.append(_wheel("pkg-1.0-py3-none-any.whl"))

        def oracle_build(w: WheelFile) -> BuildTag:
            return upstream_utils.parse_wheel_filename(w.filename)[2]

        chosen = TagSet.for_spec(
            python_version=python_version, spec=PlatformSpec("linux_x86_64")
        ).pick(wheels)
        assert chosen is not None
        assert oracle_build(chosen) == max(oracle_build(w) for w in wheels)


class TestAcceptedSpecNamesAPlatform:
    """Every accepted :class:`PlatformSpec` names at least one platform tag.

    ``packaging.tags`` reads an empty ``platforms`` argument as "unset" and
    falls back to the tags of the running host, so a spec naming no platform
    tag would resolve for nab's own machine rather than the declared one.
    """

    @given(spec=specs)
    @PROPERTY_SETTINGS
    def test_platform_tags_are_never_empty(self, spec: PlatformSpec) -> None:
        """An accepted spec always names a platform tag of its own."""
        assert _platform_tags_for_spec(spec)
