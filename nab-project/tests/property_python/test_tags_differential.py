"""Differential property tests: nab wheel selection vs upstream ``packaging.tags``.

:mod:`nab_provider.tags` implements `PEP 425`_ wheel-tag preference on top of
the vendored ``packaging.tags``. The differential classes below re-derive one
layer each with the upstream ``packaging`` distribution as the oracle and
require agreement:

1. The tag order a :class:`TagSet` builds for a declared target must
   match the same order rebuilt with upstream
   ``cpython_tags``/``compatible_tags``.
2. The vendored ``mac_platforms`` must match upstream for identical
   inputs.
3. ``parse_tag`` must expand compressed tag sets identically, and
   refuse the same strings.
4. ``wheel_tag_set`` must agree with upstream
   ``parse_wheel_filename`` on every spec-valid filename.
5. ``TagSet.pick`` must choose a wheel whose best upstream rank is
   the minimum over all candidate wheels.
6. Among wheels of equal rank, ``TagSet.pick`` must choose the one
   with the highest upstream `PEP 427`_ build tag.

.. _PEP 425: https://peps.python.org/pep-0425/
.. _PEP 427: https://peps.python.org/pep-0427/
"""

from __future__ import annotations

from types import ModuleType

import pytest
from hypothesis import given
from hypothesis import strategies as st
from packaging import tags as upstream_tags
from packaging import utils as upstream_utils
from packaging.utils import BuildTag, InvalidWheelFilename
from packaging.version import InvalidVersion

from nab_index.client import WheelFile, _parse_wheel_filename
from nab_provider._vendor.packaging import tags as vendored_tags
from nab_provider.tags import (
    _MACOS_TAG_FLOOR,
    _PLATFORM_ARCH,
    PlatformSpec,
    TagSet,
    _platform_tags_for_spec,
    wheel_tag_set,
)

from .strategies import PROPERTY_SETTINGS

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
        """Vendored tag order equals the upstream-rebuilt order."""
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


class TestMacPlatformsMatchesUpstream:
    """The vendored ``mac_platforms`` must yield the same tags, in the same
    order, as upstream ``packaging.tags.mac_platforms``.
    """

    @given(
        version=st.tuples(st.integers(10, 15), st.integers(0, 16)),
        arch=st.sampled_from(["x86_64", "arm64"]),
    )
    @PROPERTY_SETTINGS
    def test_mac_platforms_matches_upstream(
        self, version: tuple[int, int], arch: str
    ) -> None:
        """Vendored ``mac_platforms`` equals upstream for identical inputs."""
        got = list(vendored_tags.mac_platforms(version=version, arch=arch))
        expected = list(upstream_tags.mac_platforms(version=version, arch=arch))
        assert got == expected


tag_part = st.text(alphabet="abcdefgh0123456789_", min_size=1, max_size=8)
compressed = st.lists(tag_part, min_size=1, max_size=3, unique=True).map(".".join)

# packaging requires every member of a compressed interpreter set to be an
# identifier, a rule it added in 26.3 while nab-index still floors packaging at
# 24.0. So the differential draws interpreters any oracle in that range accepts,
# and TestParseTagRejects pins the rule.
compressed_interpreters = st.lists(
    tag_part.filter(str.isidentifier), min_size=1, max_size=3, unique=True
).map(".".join)


def _tag_expansion(
    module: ModuleType, tag: str
) -> frozenset[tuple[str, str, str]] | None:
    """Expand ``tag`` to its triples, or ``None`` when ``module`` rejects it.

    Each copy raises its own ``InvalidTag``, so a rejection has to fold into
    the return value to be compared.
    """
    try:
        raw = module.parse_tag(tag)
    except ValueError:
        return None
    return frozenset((t.interpreter, t.abi, t.platform) for t in raw)


@st.composite
def tag_strings(draw: st.DrawFn) -> str:
    """Draw a dash-joined tag string of three fields, or of one, two or four."""
    count = 3 if draw(st.booleans()) else draw(st.sampled_from([1, 2, 4]))
    fields = [draw(compressed_interpreters)]
    fields += [draw(compressed) for _ in range(count - 1)]
    return "-".join(fields)


class TestParseTagMatchesUpstream:
    """`PEP 425`_ compressed tag sets expand to a cross product;
    vendored ``parse_tag`` must produce the same set as upstream for
    any dotted tag string, and refuse the same strings.

    .. _PEP 425: https://peps.python.org/pep-0425/#compressed-tag-sets
    """

    @given(tag=tag_strings())
    @PROPERTY_SETTINGS
    def test_parse_tag_matches_upstream(self, tag: str) -> None:
        assert _tag_expansion(vendored_tags, tag) == _tag_expansion(upstream_tags, tag)


class TestParseTagRejects:
    """The validation the vendored ``parse_tag`` performs.

    Pinned directly rather than differentially: an oracle older than 26.3
    accepts what the vendored copy refuses.
    """

    @pytest.mark.parametrize(
        "tag",
        ["3.7-none-any", "0-0-0", "py 3-none-any", "py3.7-none-any"],
    )
    def test_rejects_non_identifier_interpreter(self, tag: str) -> None:
        """A compressed set is refused whole when one member is not an identifier."""
        with pytest.raises(vendored_tags.InvalidTag):
            vendored_tags.parse_tag(tag)

    @pytest.mark.parametrize("tag", ["py3--any", "py3-none-", "py3-none-any."])
    def test_rejects_empty_component(self, tag: str) -> None:
        with pytest.raises(vendored_tags.InvalidTag):
            vendored_tags.parse_tag(tag)

    @pytest.mark.parametrize("tag", ["py3-none", "py3-none-any-extra"])
    def test_rejects_wrong_component_count(self, tag: str) -> None:
        with pytest.raises(vendored_tags.InvalidTag):
            vendored_tags.parse_tag(tag)

    @pytest.mark.parametrize("tag", ["py3-none-any", "py2.py3-none-any", "_-none-0"])
    def test_accepts_identifier_interpreters(self, tag: str) -> None:
        """Only the interpreter field is held to the identifier rule."""
        assert vendored_tags.parse_tag(tag)


name_strategy = st.text(alphabet="abcxyz0123456789_", min_size=1, max_size=8)
version_strategy = st.sampled_from(
    ["1.0", "2.1.3", "0.9a1", "1!2.0", "3.0.post1", "1.0+local"]
)
build_strategy = st.none() | st.integers(0, 99).map(str)


@st.composite
def wheel_filenames(draw: st.DrawFn, interpreters: st.SearchStrategy[str]) -> str:
    """Generate a wheel filename with optional build tag and random tag parts.

    ``interpreters`` supplies the first tag field, letting a caller pick
    spec-valid ones or the wider alphabet.
    """
    name = draw(name_strategy)
    version = draw(version_strategy)
    build = draw(build_strategy)
    parts = [name, version]
    if build is not None:
        parts.append(build)
    parts += [draw(interpreters), draw(compressed), draw(compressed)]
    return "-".join(parts) + ".whl"


class TestWheelTagSetMatchesUpstream:
    """``wheel_tag_set`` parses the tag suffix out of wheel filenames;
    on every filename upstream ``parse_wheel_filename`` accepts, the
    extracted tag set must agree.  Filenames upstream rejects are out
    of scope: nab is intentionally permissive there.
    """

    @given(filename=wheel_filenames(compressed_interpreters))
    @PROPERTY_SETTINGS
    def test_wheel_tag_set_matches_upstream_parse_wheel_filename(
        self, filename: str
    ) -> None:
        """``wheel_tag_set`` agrees with upstream on spec-valid filenames."""
        try:
            _, _, _, expected = upstream_utils.parse_wheel_filename(filename)
        except (InvalidWheelFilename, InvalidVersion):
            return  # upstream rejects; nab is intentionally permissive
        got = wheel_tag_set(filename)
        assert got is not None
        assert {(t.interpreter, t.abi, t.platform) for t in got} == {
            (t.interpreter, t.abi, t.platform) for t in expected
        }


class TestAdmittedWheelsCarryTags:
    """A wheel nab_index admits must be one ``nab_provider.tags`` can rank.

    nab_index applies its own copy of the tag rules and nab_provider runs the
    vendored ``parse_tag``, so re-vendoring a stricter one is how the two come
    apart. The interpreter field draws the wide alphabet to reach that.
    """

    @given(filename=wheel_filenames(compressed))
    @PROPERTY_SETTINGS
    def test_readable_wheel_has_tags(self, filename: str) -> None:
        if _parse_wheel_filename(filename) is None:
            return
        assert wheel_tag_set(filename) is not None


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
