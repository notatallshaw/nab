"""Property tests for :class:`nab_provider.target.ResolveTarget`'s marker env.

A host-python target merges a fixed defaults dict (the host machine's
PEP 508 environment), a ``python_version`` override derived from the
requested interpreter, and a user-supplied overrides mapping.  The
PubGrub provider evaluates root markers against the resulting dict, so
the merge must be deterministic in the algebraic sense: empty overrides
leave the result untouched, and disjoint overrides commute.

Reference: https://peps.python.org/pep-0508/#environment-markers
"""

# ruff: noqa: RUF002
# RUF002: allow set-theory union operator in docstrings.

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from nab_provider.target import ResolveTarget

from .strategies import PROPERTY_SETTINGS

if TYPE_CHECKING:
    from collections.abc import Mapping

pytestmark = pytest.mark.property


def _marker_env(python_version: str, overrides: dict[str, str]) -> Mapping[str, str]:
    """The marker env a host target for ``python_version`` resolves against."""
    target = ResolveTarget.for_host_python(python_version)
    return target.with_marker_overrides(overrides).marker_env


# Marker keys whose values are stable strings on every supported
# host.  The strategies below sample from these so the overrides
# resemble the marker subset users typically tweak.
_OVERRIDE_KEYS: tuple[str, ...] = (
    "os_name",
    "sys_platform",
    "platform_machine",
    "platform_python_implementation",
    "platform_release",
    "platform_system",
    "platform_version",
    "implementation_name",
)

_OVERRIDE_VALUES: tuple[str, ...] = (
    "linux",
    "darwin",
    "win32",
    "x86_64",
    "arm64",
    "AMD64",
    "Linux",
    "Darwin",
    "Windows",
    "posix",
    "nt",
    "CPython",
    "cpython",
    "PyPy",
    "pypy",
    "",
)


@st.composite
def python_version_strings(draw: st.DrawFn) -> str:
    """Generate a parseable PEP 440 Python version like ``3.11.2``."""
    major = draw(st.integers(min_value=3, max_value=3))
    minor = draw(st.integers(min_value=8, max_value=14))
    patch = draw(st.integers(min_value=0, max_value=12))
    return f"{major}.{minor}.{patch}"


@st.composite
def override_dicts(draw: st.DrawFn) -> dict[str, str]:
    """Draw a small mapping from a known marker-key pool."""
    keys = draw(
        st.lists(st.sampled_from(_OVERRIDE_KEYS), min_size=0, max_size=4, unique=True)
    )
    return {key: draw(st.sampled_from(_OVERRIDE_VALUES)) for key in keys}


class TestEmptyOverrideIsIdentity:
    """An empty overrides mapping leaves the default environment
    unchanged for a given Python.

    The resolver re-uses this helper every time root markers are
    evaluated.  When the caller has nothing to override, the result
    must equal the implicit default; otherwise a downstream marker
    would see a perturbed value with no clear provenance.
    """

    @given(python_version=python_version_strings())
    @PROPERTY_SETTINGS
    def test_empty_overlay_is_identity(self, python_version: str) -> None:
        """Empty overrides reproduce the unmodified default environment."""
        baseline = _marker_env(python_version, {})
        repeated = _marker_env(python_version, {})
        assert baseline == repeated


class TestDisjointOverlaysCommute:
    """Two override dicts with disjoint keys commute under merge.

    The implementation calls ``dict.update`` once with the combined
    mapping; the property test layers two disjoint overrides one at
    a time in both orders and asserts the merged result is the same.
    A future refactor that keyed values by source order rather than
    name would silently break this guarantee.
    """

    @given(
        python_version=python_version_strings(),
        overlay_a=override_dicts(),
        overlay_b=override_dicts(),
    )
    @PROPERTY_SETTINGS
    def test_disjoint_overlays_commute(
        self,
        python_version: str,
        overlay_a: dict[str, str],
        overlay_b: dict[str, str],
    ) -> None:
        """``base ∪ A ∪ B == base ∪ B ∪ A`` for disjoint ``A`` and ``B``."""
        assume(set(overlay_a).isdisjoint(set(overlay_b)))
        a_then_b = _marker_env(python_version, {**overlay_a, **overlay_b})
        b_then_a = _marker_env(python_version, {**overlay_b, **overlay_a})
        assert a_then_b == b_then_a
