"""Property tests for the matrix-expansion logic in :mod:`nab_python.target`.

The matrix expansion produces one ``ResolveTarget`` per
``(python_version, platform_id)`` pair admitted by a PEP 440
specifier.  This file walks the relevant clauses of `PEP 440`_,
`PEP 425`_, and `PEP 508`_ paragraph by paragraph and adds a
property test for each one.  PEP 508 specifies the
environment-marker variables every tool must provide; the matrix's
per-tuple ``marker_env`` dict must include every PEP 508 key so
that subsequent ``Marker.evaluate`` calls do not raise.

.. _PEP 440: https://peps.python.org/pep-0440/
.. _PEP 425: https://peps.python.org/pep-0425/
.. _PEP 508: https://peps.python.org/pep-0508/#environment-markers
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_python._vendor.packaging.markers import Marker
from nab_python._vendor.packaging.specifiers import SpecifierSet
from nab_python._vendor.packaging.version import Version
from nab_python.tags import PlatformSpec
from nab_python.target import (
    KNOWN_PYTHON_MINORS,
    PLATFORM_MARKERS,
    Matrix,
    _pythons_in_range,
)

from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

PLATFORM_IDS = tuple(PLATFORM_MARKERS)


@st.composite
def python_specs_admitting_some_minor(draw: st.DrawFn) -> str:
    """Generate a PEP 440 spec admitting at least one known minor.

    Random specifiers can intersect to empty; we constrain to bounded
    ranges that include at least one ``KNOWN_PYTHON_MINORS`` value
    so that expansion is non-trivial.
    """
    lo_minor_index = draw(
        st.integers(min_value=0, max_value=len(KNOWN_PYTHON_MINORS) - 1)
    )
    hi_minor_index = draw(
        st.integers(min_value=lo_minor_index + 1, max_value=len(KNOWN_PYTHON_MINORS))
    )
    lo = KNOWN_PYTHON_MINORS[lo_minor_index]
    if hi_minor_index >= len(KNOWN_PYTHON_MINORS):
        return f">={lo}"
    hi = KNOWN_PYTHON_MINORS[hi_minor_index]
    return f">={lo}, <{hi}"


@st.composite
def platform_subsets(draw: st.DrawFn) -> tuple[PlatformSpec, ...]:
    """Sample a non-empty subset of the known platforms, at their defaults."""
    chosen = draw(
        st.lists(
            st.sampled_from(PLATFORM_IDS),
            min_size=1,
            max_size=len(PLATFORM_IDS),
            unique=True,
        )
    )
    return tuple(PlatformSpec(platform_id) for platform_id in chosen)


class TestCartesianCardinality:
    """`PEP 440`_ describes the version specifier semantics that drive
    matrix admission; the matrix produces the Cartesian product of
    admitted Python minors and platforms, so
    ``|tuples| == |pythons_in_range| * |platforms|``.

    A wrong cardinality usually reveals an off-by-one in the
    minor-bounds inclusion or a duplicate-tuple bug.

    .. _PEP 440: https://peps.python.org/pep-0440/#version-specifiers
    """

    @given(spec=python_specs_admitting_some_minor(), platforms=platform_subsets())
    @PROPERTY_SETTINGS
    def test_cardinality_equals_pythons_times_platforms(
        self, spec: str, platforms: tuple[PlatformSpec, ...]
    ) -> None:
        """``|tuples|`` is the cross-product of admitted Pythons and platforms."""
        matrix = Matrix(python=spec, platforms=platforms)
        tuples = matrix.expand()
        parsed = SpecifierSet(spec)
        admitted = [m for m in KNOWN_PYTHON_MINORS if Version(f"{m}.0") in parsed]
        assert len(tuples) == len(admitted) * len(platforms)


class TestExpansionDeterminism:
    """`PEP 751`_ does not mandate determinism but recommends it for
    diff-friendly lockfiles.  ``Matrix.expand()`` must therefore be a
    pure function of its inputs and produce the same sequence on
    every call.

    .. _PEP 751: https://peps.python.org/pep-0751/
    """

    @given(spec=python_specs_admitting_some_minor(), platforms=platform_subsets())
    @PROPERTY_SETTINGS
    def test_expand_is_idempotent(
        self, spec: str, platforms: tuple[PlatformSpec, ...]
    ) -> None:
        """Two ``expand()`` calls on the same Matrix produce equal sequences."""
        matrix = Matrix(python=spec, platforms=platforms)
        first = matrix.expand()
        second = matrix.expand()
        assert [(t.python_version, t.platform_id) for t in first] == [
            (t.python_version, t.platform_id) for t in second
        ]

    @given(spec=python_specs_admitting_some_minor(), platforms=platform_subsets())
    @PROPERTY_SETTINGS
    def test_expand_environment_dict_stable(
        self, spec: str, platforms: tuple[PlatformSpec, ...]
    ) -> None:
        """The per-tuple ``marker_env`` dict is identical across calls.

        The label/identity check in :meth:`test_expand_is_idempotent`
        confirms tuple ordering is deterministic.  Downstream marker
        evaluation, however, reads from ``environment``: a non-stable
        dict (for example one that picks default values from a
        non-deterministic source) would break per-call reproducibility
        even when labels matched.
        """
        matrix = Matrix(python=spec, platforms=platforms)
        first = matrix.expand()
        second = matrix.expand()
        assert len(first) == len(second)
        for tup_a, tup_b in zip(first, second, strict=True):
            assert tup_a.marker_env == tup_b.marker_env


class TestPythonOrderingFlip:
    """The matrix supports ``python_order='asc'`` (ascending Python
    order, default) and ``python_order='desc'`` (descending Python
    order, platform order preserved within each Python row).
    Anything else must raise ``ValueError``.

    The iteration order matters because the first tuple in the
    sequence is what the universal-resolve algorithm treats as the
    primary, against which other tuples are aligned.
    """

    @given(spec=python_specs_admitting_some_minor(), platforms=platform_subsets())
    @PROPERTY_SETTINGS
    def test_desc_is_reverse_of_asc(
        self, spec: str, platforms: tuple[PlatformSpec, ...]
    ) -> None:
        """``desc`` reverses the Python axis but keeps platform order."""
        asc = Matrix(python=spec, platforms=platforms).expand()
        desc = Matrix(python=spec, platforms=platforms, python_order="desc").expand()
        asc_pys = [t.python_version for t in asc[:: len(platforms)]]
        desc_pys = [t.python_version for t in desc[:: len(platforms)]]
        assert desc_pys == list(reversed(asc_pys))

    @given(
        bad_order=st.text(min_size=1, max_size=10).filter(
            lambda s: s not in {"asc", "desc"}
        ),
    )
    @PROPERTY_SETTINGS
    def test_invalid_python_order_always_raises(self, bad_order: str) -> None:
        """Any other ``python_order`` value raises ``ValueError``."""
        with pytest.raises(ValueError, match="python_order"):
            Matrix(
                python=">=3.10",
                platforms=(PlatformSpec("linux_x86_64"),),
                python_order=bad_order,
            ).expand()


class TestPythonsInRange:
    """The helper that maps a PEP 440 specifier to known minor strings.

    ``_pythons_in_range`` underpins :meth:`Matrix.expand`'s Python
    axis.  Three guarantees are load-bearing:

    1. Every output is an ``M.N`` minor string drawn from
       :data:`KNOWN_PYTHON_MINORS`.  Downstream consumers
       (marker evaluation, wheel-tag selection) depend on this
       shape; emitting ``M.N.P`` patch strings would break tag
       generation.
    2. The list is ascending and unique.  Duplicate or out-of-order
       entries would inflate the matrix and silently change
       cross-tuple alignment ordering.
    3. Output order matches the canonical ascending order in
       :data:`KNOWN_PYTHON_MINORS`.  Callers rely on this to keep
       lockfile output diff-friendly.
    """

    @given(spec=python_specs_admitting_some_minor())
    @PROPERTY_SETTINGS
    def test_output_is_minor_strings_sorted_unique(self, spec: str) -> None:
        """Every output is an ``M.N`` known minor; the list is sorted and unique."""
        result = list(_pythons_in_range(spec))
        assert len(result) == len(set(result))
        for minor in result:
            parts = minor.split(".")
            assert len(parts) == 2
            assert all(part.isdigit() for part in parts)
            assert minor in KNOWN_PYTHON_MINORS

        # Order matches the canonical KNOWN_PYTHON_MINORS order.
        canonical_index = {m: i for i, m in enumerate(KNOWN_PYTHON_MINORS)}
        indices = [canonical_index[m] for m in result]
        assert indices == sorted(indices)


class TestQuotePEP508MarkerKeys:
    """PEP 508, § Environment markers, lists the supported variables
    in a table whose ``Marker`` column is:

    > ``os_name``, ``sys_platform``, ``platform_machine``,
    > ``platform_python_implementation``, ``platform_release``,
    > ``platform_system``, ``platform_version``, ``python_version``,
    > ``python_full_version``, ``implementation_name``,
    > ``implementation_version``, ``extra``.

    Every tuple's environment dict must include all of these keys
    (excluding ``extra``, which is context-supplied) so that
    subsequent ``Marker.evaluate`` calls do not raise.

    Reference: https://peps.python.org/pep-0508/#environment-markers
    """

    @given(spec=python_specs_admitting_some_minor(), platforms=platform_subsets())
    @PROPERTY_SETTINGS
    def test_every_tuple_has_required_marker_keys(
        self, spec: str, platforms: tuple[PlatformSpec, ...]
    ) -> None:
        """Every per-tuple environment has the full PEP 508 key set."""
        required = {
            "python_version",
            "python_full_version",
            "implementation_name",
            "implementation_version",
            "os_name",
            "platform_machine",
            "platform_python_implementation",
            "platform_release",
            "platform_system",
            "platform_version",
            "sys_platform",
        }
        for tup in Matrix(python=spec, platforms=platforms).expand():
            missing = required - tup.marker_env.keys()
            assert not missing, f"{tup.label} missing {missing}"


class TestQuoteSysPlatformConsistency:
    """PEP 508, § Environment markers, ``sys_platform`` row:

    > ``sys_platform``, ``sys.platform``, sample values:
    > ``linux``, ``linux2``, ``darwin``, ``java1.8.0_51`` (note
    > that ``linux`` is from Python3 and ``linux2`` from Python2).

    The ``marker_env`` dict's ``sys_platform`` must be consistent
    with the tuple's ``platform_id``.  A regression in
    ``PLATFORM_MARKERS`` (e.g. setting ``sys_platform`` to
    ``"Darwin"`` for a macOS id) would mis-evaluate platform-gated
    markers.

    Reference: https://peps.python.org/pep-0508/#environment-markers
    """

    @given(platforms=platform_subsets())
    @PROPERTY_SETTINGS
    def test_sys_platform_marker_consistent_with_platform_id(
        self, platforms: tuple[PlatformSpec, ...]
    ) -> None:
        """``sys_platform == 'X'`` evaluates True iff ``platform_id`` matches."""
        matrix = Matrix(python="==3.11", platforms=platforms)
        linux_marker = Marker("sys_platform == 'linux'")
        win_marker = Marker("sys_platform == 'win32'")
        darwin_marker = Marker("sys_platform == 'darwin'")
        for tup in matrix.expand():
            is_linux = tup.platform_id.startswith("linux_")
            is_win = tup.platform_id.startswith("windows_")
            is_mac = tup.platform_id.startswith("macos_")
            assert linux_marker.evaluate(tup.marker_env) is is_linux
            assert win_marker.evaluate(tup.marker_env) is is_win
            assert darwin_marker.evaluate(tup.marker_env) is is_mac

    @given(spec=python_specs_admitting_some_minor(), platforms=platform_subsets())
    @PROPERTY_SETTINGS
    def test_python_version_marker_matches_axis(
        self, spec: str, platforms: tuple[PlatformSpec, ...]
    ) -> None:
        """``python_version == X`` evaluates True iff ``X`` is the tuple's value."""
        matrix = Matrix(python=spec, platforms=platforms)
        for tup in matrix.expand():
            marker = Marker(f"python_version == '{tup.python_version}'")
            assert marker.evaluate(tup.marker_env) is True
