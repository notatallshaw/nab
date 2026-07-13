"""Property tests for per-extra dependency extraction in :mod:`nab_python.universal.validate`.

The validate phase compares the dependency lists from the resolver
against the actual METADATA artifacts.  The pure helpers
``_evaluate_metadata_deps_by_extra`` and ``_per_extra_divergence``
drive that comparison.

This file walks the relevant clauses of the `Core Metadata
Specification`_, `PEP 508`_, and `PEP 685`_ paragraph by paragraph
and adds a property test for each invariant the helpers must
preserve.

.. _Core Metadata Specification: https://packaging.python.org/en/latest/specifications/core-metadata/
.. _PEP 508: https://peps.python.org/pep-0508/
.. _PEP 685: https://peps.python.org/pep-0685/
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python.universal.validate import (
    _evaluate_metadata_deps_by_extra,
    _per_extra_divergence,
)

from .strategies import LINUX_TARGET, PROPERTY_SETTINGS

pytestmark = pytest.mark.property

_DEP_NAMES = ("requests", "numpy", "pandas", "scipy", "pyarrow", "tqdm")
_EXTRA_NAMES = ("redis", "postgres", "test", "dev")


@st.composite
def metadata_text(draw: st.DrawFn) -> tuple[str, dict[str | None, set[str]]]:
    """Build a METADATA text plus the expected ``{bucket: deps}`` mapping."""
    extras = draw(
        st.lists(st.sampled_from(_EXTRA_NAMES), unique=True, min_size=0, max_size=4)
    )
    n_base = draw(st.integers(min_value=0, max_value=4))
    base_deps = draw(
        st.lists(
            st.sampled_from(_DEP_NAMES), unique=True, min_size=n_base, max_size=n_base
        )
    )
    extra_deps_map: dict[str, list[str]] = {}
    for extra in extras:
        n = draw(st.integers(min_value=0, max_value=3))
        extra_deps_map[extra] = draw(
            st.lists(st.sampled_from(_DEP_NAMES), unique=True, min_size=n, max_size=n)
        )

    lines = ["Metadata-Version: 2.1", "Name: pkg", "Version: 1.0"]
    lines.extend(f"Provides-Extra: {extra}" for extra in extras)
    lines.extend(f"Requires-Dist: {dep}" for dep in base_deps)
    for extra, deps in extra_deps_map.items():
        lines.extend(f'Requires-Dist: {dep}; extra == "{extra}"' for dep in deps)
    text = "\n".join(lines) + "\n\n"
    expected: dict[str | None, set[str]] = {
        None: {canonicalize_name(d) for d in base_deps}
    }
    for extra, deps in extra_deps_map.items():
        expected[canonicalize_name(extra)] = {canonicalize_name(d) for d in deps}
    return text, expected


class TestEvaluateGroupsByExtra:
    """The `Core Metadata Specification`_ defines ``Provides-Extra``
    as the field that declares an extra and ``Requires-Dist; extra
    == "X"`` as the conditional dependency syntax: a requirement
    with that marker applies only when extra ``X`` is requested,
    otherwise the requirement is part of the base dependency set.

    The evaluator must split ``Requires-Dist`` lines into a base
    bucket plus one bucket per declared extra.

    .. _Core Metadata Specification:
       https://packaging.python.org/en/latest/specifications/core-metadata/#provides-extra-multiple-use
    """

    @given(payload=metadata_text())
    @PROPERTY_SETTINGS
    def test_evaluate_groups_match_expected(
        self, payload: tuple[str, dict[str | None, set[str]]]
    ) -> None:
        """Computed buckets equal the buckets we encoded in the METADATA."""
        text, expected = payload
        out = _evaluate_metadata_deps_by_extra(text, LINUX_TARGET.marker_env)
        assert out == expected


class TestPerExtraDivergenceReflexive:
    """Reflexivity of the divergence relation: a metadata file has no
    extra/missing deps with respect to itself.  Stated formally:
    ``divergence(M, M) == ∅``.

    A regression here would mean the validator falsely reports
    drift on every package even when nothing is wrong.
    """

    @given(payload=metadata_text())
    @PROPERTY_SETTINGS
    def test_identical_metadata_no_per_extra_diff(
        self, payload: tuple[str, dict[str | None, set[str]]]
    ) -> None:
        """``divergence(M, M) == ∅``."""
        _text, by_extra = payload
        diffs = _per_extra_divergence(by_extra, by_extra)
        assert diffs == ()


class TestPerExtraDivergenceSymmetricUnderSwap:
    """The divergence relation is symmetric in the structural sense: if
    dep ``D`` is "extra" in ``divergence(A, B)`` it is "missing" in
    ``divergence(B, A)`` and vice versa.

    Important so the validator's diagnostics are independent of
    which side we treat as "expected".
    """

    @given(payload=metadata_text())
    @PROPERTY_SETTINGS
    def test_per_extra_divergence_symmetric_under_swap(
        self, payload: tuple[str, dict[str | None, set[str]]]
    ) -> None:
        """``divergence(A, B)`` and ``divergence(B, A)`` swap extra_deps/missing_deps."""
        _text, by_extra = payload
        perturbed: dict[str | None, set[str]] = {k: set(v) for k, v in by_extra.items()}
        if perturbed:
            first_extra = next(iter(k for k in perturbed if k is not None), None)
            if first_extra is not None:
                perturbed[first_extra].add("ghostlib")
                forward = _per_extra_divergence(perturbed, by_extra)
                reverse = _per_extra_divergence(by_extra, perturbed)
                assert len(forward) == len(reverse)
                for forward_diff, reverse_diff in zip(forward, reverse, strict=True):
                    assert forward_diff.extra == reverse_diff.extra
                    assert forward_diff.extra_deps == reverse_diff.missing_deps
                    assert forward_diff.missing_deps == reverse_diff.extra_deps
