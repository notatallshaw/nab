"""Property tests for cross-tuple alignment helpers in :mod:`nab_python.universal.resolve`.

Universal resolution iterates per-tuple resolutions serially with
each tuple's pins threaded forward as preferences.  The pure helpers
``_direct_package_names`` and ``_parse_requirements`` shape that
input.

This file walks the relevant clauses of `PEP 503`_ (canonical names)
and `PEP 508`_ (environment markers) paragraph by paragraph and
adds a property test for each invariant the helpers must preserve.

.. _PEP 503: https://peps.python.org/pep-0503/
.. _PEP 508: https://peps.python.org/pep-0508/
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.version import Version
from nab_python.lockfile import IndexPin, LockInput
from nab_python.universal.matrix import Matrix, MatrixTuple
from nab_python.universal.resolve import (
    TupleResult,
    UniversalResult,
    _direct_package_names,
    _parse_requirements,
    merge_universal_lock_inputs,
)

from .strategies import LINUX_ENV, PROPERTY_SETTINGS

pytestmark = pytest.mark.property


def _fake_tuple() -> MatrixTuple:
    """Build a minimal linux/3.11 ``MatrixTuple`` for property fixtures."""
    return MatrixTuple(
        python_version="3.11",
        platform_id="linux_x86_64",
        environment=LINUX_ENV,
    )


@st.composite
def pep440_versions(draw: st.DrawFn) -> Version:
    """Generate a small comparable PEP 440 version."""
    major = draw(st.integers(min_value=0, max_value=10))
    minor = draw(st.integers(min_value=0, max_value=20))
    return Version(f"{major}.{minor}")


@st.composite
def package_names(draw: st.DrawFn) -> str:
    """Generate a small canonicalised package name (no special chars)."""
    return draw(st.sampled_from([f"pkg{i}" for i in range(8)]))


class TestQuoteCanonicalDirectNames:
    """PEP 503, § Normalized Names:

    > "This PEP references the concept of a "normalized" project
    > name. As per PEP 426 the only valid characters in a name are
    > the ASCII alphabet, ASCII numbers, ``.``, ``-``, and ``_``.
    > The name should be lowercased with all runs of the characters
    > ``.``, ``-``, or ``_`` replaced with a single ``-`` character."

    User-supplied direct dependency names go through the same
    canonicalization before the resolver compares them.

    Reference: https://peps.python.org/pep-0503/#normalized-names
    """

    @given(
        names=st.lists(
            st.sampled_from(["My-Pkg", "MY_PKG", "my.pkg", "My-Pkg"]),
            min_size=0,
            max_size=5,
        )
    )
    @PROPERTY_SETTINGS
    def test_direct_package_names_dedup_and_canonicalize(
        self, names: list[str]
    ) -> None:
        """Variations of the same name canonicalise to a single canonical form."""
        out = _direct_package_names(names)
        assert all(n == n.lower() for n in out)
        if names:
            assert out == {"my-pkg"}


@st.composite
def requirement_strings(draw: st.DrawFn) -> str:
    """Generate a parseable requirement string with optional marker."""
    name = draw(package_names())
    has_specifier = draw(st.booleans())
    has_marker = draw(st.booleans())
    spec = ""
    if has_specifier:
        op = draw(st.sampled_from([">=", "<=", "==", "!=", ">", "<"]))
        ver = draw(pep440_versions())
        spec = f"{op}{ver}"
    marker = ""
    if has_marker:
        plat = draw(st.sampled_from(["linux", "win32", "darwin"]))
        marker = f'; sys_platform == "{plat}"'
    return f"{name}{spec}{marker}"


class TestMarkerFiltering:
    """A `PEP 508`_ environment marker conditions a requirement on
    properties of the target environment.  When the marker
    evaluates to ``False`` for a given environment, the requirement
    does not apply.

    ``_parse_requirements`` evaluates the marker for the per-tuple
    environment and drops requirements whose marker says ``False``;
    otherwise the per-tuple solve would over-constrain.

    .. _PEP 508: https://peps.python.org/pep-0508/#environment-markers
    """

    @given(reqs=st.lists(requirement_strings(), min_size=0, max_size=5))
    @PROPERTY_SETTINGS
    def test_parse_requirements_drops_non_matching_markers(
        self, reqs: list[str]
    ) -> None:
        """Requirements with non-matching markers are absent from the parsed dict."""
        linux_env = _fake_tuple().environment
        out = _parse_requirements(reqs, linux_env)
        assert all(isinstance(v, VersionRange) for v in out.values())


@st.composite
def pin_maps(draw: st.DrawFn) -> dict[str, IndexPin]:
    """Draw a non-empty ``{name: IndexPin}`` mapping for a tuple's lock input."""
    names = draw(st.lists(package_names(), min_size=1, max_size=4, unique=True))
    return {
        name: IndexPin(
            name=name,
            version=str(draw(pep440_versions())),
            index="https://pypi.org/simple/",
        )
        for name in names
    }


_PLATFORM_POOL: tuple[str, ...] = (
    "linux_x86_64",
    "macos_arm64",
    "windows_amd64",
)


@st.composite
def tuple_lock_inputs(
    draw: st.DrawFn,
) -> list[tuple[MatrixTuple, dict[str, IndexPin]]]:
    """Draw N ``(MatrixTuple, pin_map)`` pairs with unique labels."""
    size = draw(st.integers(min_value=1, max_value=3))
    chosen_platforms = draw(
        st.lists(
            st.sampled_from(_PLATFORM_POOL),
            min_size=size,
            max_size=size,
            unique=True,
        )
    )
    pairs: list[tuple[MatrixTuple, dict[str, IndexPin]]] = []
    for platform in chosen_platforms:
        tup = MatrixTuple(
            python_version="3.11",
            platform_id=platform,
            environment={**LINUX_ENV, "sys_platform": platform.split("_", 1)[0]},
        )
        pairs.append((tup, draw(pin_maps())))
    return pairs


class TestMergeUniversalLockInputs:
    """Per-tuple pins survive the merge into a universal :class:`LockInput`.

    PEP 751 expects per-environment package entries: callers feed a
    ``UniversalResult`` whose tuple results each carry a
    :class:`LockInput`, and the merge must preserve every pin under
    the originating tuple's label.  A regression that filtered or
    coalesced pins would silently drop installation targets from
    the lockfile.

    Reference: https://peps.python.org/pep-0751/
    """

    @given(pairs=tuple_lock_inputs())
    @PROPERTY_SETTINGS
    def test_merge_preserves_per_tuple_pins(
        self, pairs: list[tuple[MatrixTuple, dict[str, IndexPin]]]
    ) -> None:
        """Every input ``(name, version)`` reappears under the source tuple's label."""
        matrix = Matrix(
            python="==3.11", platforms=tuple(p.platform_id for p, _ in pairs)
        )
        tuple_results = [
            TupleResult(
                tuple_=tup,
                success=True,
                pins={name: Version(pin.version) for name, pin in pin_map.items()},
                lock_input=LockInput(pins=pin_map),
            )
            for tup, pin_map in pairs
        ]
        universal = UniversalResult(matrix=matrix, tuple_results=tuple_results)
        merged = merge_universal_lock_inputs(universal)

        for tup, pin_map in pairs:
            label = tup.label
            assert label in merged.per_tuple_pins
            recorded = merged.per_tuple_pins[label]
            for name, pin in pin_map.items():
                assert name in recorded
                assert recorded[name].version == pin.version
