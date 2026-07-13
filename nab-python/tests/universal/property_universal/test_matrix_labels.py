"""Matrix-expansion coverage and label-injectivity property tests.

Tuple labels are used downstream as dict keys for per-tuple pins, so
two distinct matrix points must never share a label
(:mod:`nab_python.universal.matrix` docstrings make this claim for
both selections and platform specs).  A collision silently merges
two tuples' pins under one key.
"""

from __future__ import annotations

import itertools

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from nab_python.tags import PlatformSpec
from nab_python.universal.matrix import (
    _IMPLEMENTATION_DEFAULTS,
    _KNOWN_PYTHON_MINORS,
    _PLATFORM_DEFAULTS,
    Matrix,
)

from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

PLATFORM_IDS = sorted(_PLATFORM_DEFAULTS)
IMPLS = sorted(_IMPLEMENTATION_DEFAULTS)

python_specs = st.one_of(
    st.sampled_from(_KNOWN_PYTHON_MINORS).map(lambda v: f"=={v}"),
    st.sampled_from(_KNOWN_PYTHON_MINORS).map(lambda v: f">={v}"),
    st.sampled_from(_KNOWN_PYTHON_MINORS).map(lambda v: f"~={v}"),
    st.tuples(
        st.sampled_from(_KNOWN_PYTHON_MINORS), st.sampled_from(_KNOWN_PYTHON_MINORS)
    ).map(lambda t: f">={t[0]}, <={t[1]}"),
    st.sampled_from(_KNOWN_PYTHON_MINORS).map(lambda v: f"!={v}"),
)

# No whitespace: label_suffix collapses runs of it into "_", which collides
# with a literal underscore.
rel_strings = st.text(alphabet="_-abc0123456789.", min_size=0, max_size=8)

libc_versions = st.none() | st.tuples(st.integers(0, 3), st.integers(0, 20))


def _specs_for(platform_id: str) -> st.SearchStrategy[PlatformSpec]:
    """Build a ``PlatformSpec`` strategy fixed to one platform id."""
    return st.builds(
        PlatformSpec,
        platform_id=st.just(platform_id),
        libc=st.sampled_from(["glibc", "musl"]),
        libc_version=libc_versions,
        macos_min=st.none() | st.tuples(st.integers(10, 15), st.integers(0, 3)),
        platform_release=rel_strings,
        platform_version=rel_strings,
        free_threaded=st.booleans(),
    )


@st.composite
def spec_pairs(draw: st.DrawFn) -> tuple[PlatformSpec, PlatformSpec]:
    """Generate two specs sharing a platform id."""
    platform_id = draw(st.sampled_from(PLATFORM_IDS))
    return draw(_specs_for(platform_id)), draw(_specs_for(platform_id))


class TestExpansionExactCoverage:
    """``Matrix.expand()`` must produce exactly the Cartesian product
    of admitted Python minors, platforms, and implementations, with
    deterministic per-tuple environments and pairwise-distinct
    labels, for every supported spec shape and ordering.
    """

    @given(
        python=python_specs,
        platforms=st.lists(
            st.sampled_from(PLATFORM_IDS), min_size=1, max_size=5, unique=True
        ),
        impls=st.lists(st.sampled_from(IMPLS), min_size=1, max_size=2, unique=True),
        order=st.sampled_from(["asc", "desc"]),
    )
    @PROPERTY_SETTINGS
    def test_expansion_exact_coverage_and_determinism(
        self, python: str, platforms: list[str], impls: list[str], order: str
    ) -> None:
        """Expansion covers the exact cross product, repeatably, with unique labels."""
        matrix = Matrix(
            python=python,
            platforms=tuple(platforms),
            python_order=order,
            implementations=tuple(impls),
        )
        try:
            tuples = matrix.expand()
        except ValueError:
            return  # empty python range; validated separately
        pythons = {t.python_version for t in tuples}
        expected = set(itertools.product(pythons, platforms, impls))
        got = [(t.python_version, t.platform_id, t.implementation) for t in tuples]
        assert len(got) == len(expected)
        assert set(got) == expected
        again = matrix.expand()
        assert [(t.label, t.environment) for t in again] == [
            (t.label, t.environment) for t in tuples
        ]
        labels = [t.label for t in tuples]
        assert len(set(labels)) == len(labels), labels


class TestLabelSuffixInjective:
    """``PlatformSpec.label_suffix`` is the documented disambiguator
    for specs sharing a ``platform_id``: distinct specs must render
    distinct suffixes, otherwise their per-tuple pins silently merge
    under one dict key.
    """

    @given(pair=spec_pairs())
    @PROPERTY_SETTINGS
    def test_label_suffix_injective_for_distinct_specs(
        self, pair: tuple[PlatformSpec, PlatformSpec]
    ) -> None:
        """Two distinct same-platform specs never share a label suffix."""
        spec_a, spec_b = pair
        assume(spec_a != spec_b)
        assert spec_a.label_suffix() != spec_b.label_suffix(), (
            f"{spec_a!r} and {spec_b!r} collide on suffix {spec_a.label_suffix()!r}"
        )


class TestPythonPatchesEnvironment:
    """A ``python_patches`` override pins the tuple's full version;
    ``python_full_version`` and ``implementation_version`` must carry
    the override while ``python_version`` stays the minor.
    """

    @given(
        minor=st.sampled_from(_KNOWN_PYTHON_MINORS),
        patch=st.integers(0, 20),
    )
    @PROPERTY_SETTINGS
    def test_python_patches_set_full_version_consistently(
        self, minor: str, patch: int
    ) -> None:
        """The patch override flows to every full-version environment key."""
        full = f"{minor}.{patch}"
        matrix = Matrix(
            python=f"=={minor}",
            platforms=("linux_x86_64",),
            python_patches={minor: full},
        )
        (tup,) = matrix.expand()
        env = tup.environment
        assert env["python_full_version"] == full
        assert env["python_version"] == minor
        assert env["implementation_version"] == full
