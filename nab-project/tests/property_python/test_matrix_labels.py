"""Matrix-expansion coverage and label-injectivity property tests.

Tuple labels are used downstream as dict keys for per-tuple pins, so
two distinct matrix points must never share a label
(:mod:`nab_provider.target` docstrings make this claim for
both selections and platform specs).  A collision silently merges
two tuples' pins under one key.
"""

from __future__ import annotations

import itertools

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from nab_provider.tags import (
    _MACOS_TAG_FLOOR,
    _PLATFORM_ARCH,
    _PLATFORM_KIND,
    LIBC_MAJOR,
    PlatformSpec,
)
from nab_provider.target import (
    IMPLEMENTATION_MARKERS,
    KNOWN_PYTHON_MINORS,
    PLATFORM_MARKERS,
    Matrix,
)

from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

PLATFORM_IDS = sorted(PLATFORM_MARKERS)
IMPLS = sorted(IMPLEMENTATION_MARKERS)

python_specs = st.one_of(
    st.sampled_from(KNOWN_PYTHON_MINORS).map(lambda v: f"=={v}"),
    st.sampled_from(KNOWN_PYTHON_MINORS).map(lambda v: f">={v}"),
    st.sampled_from(KNOWN_PYTHON_MINORS).map(lambda v: f"~={v}"),
    st.tuples(
        st.sampled_from(KNOWN_PYTHON_MINORS), st.sampled_from(KNOWN_PYTHON_MINORS)
    ).map(lambda t: f">={t[0]}, <={t[1]}"),
    st.sampled_from(KNOWN_PYTHON_MINORS).map(lambda v: f"!={v}"),
)

# The alphabet a real platform_release or platform_version draws from, which
# also keeps a shrunk counterexample readable.
rel_strings = st.text(alphabet="_-abc0123456789.", min_size=0, max_size=8)


def _libc_knobs(platform_id: str) -> st.SearchStrategy[dict[str, object]]:
    """Draw the libc knobs the platform admits: none unless it is Linux."""
    if _PLATFORM_KIND[platform_id] != "linux":
        return st.just({})
    return st.sampled_from(sorted(LIBC_MAJOR)).flatmap(
        lambda libc: st.fixed_dictionaries(
            {
                "libc": st.just(libc),
                "runs_on_libc": st.none()
                | st.tuples(st.just(LIBC_MAJOR[libc]), st.integers(0, 20)),
            }
        )
    )


def _macos_knobs(platform_id: str) -> st.SearchStrategy[dict[str, object]]:
    """Draw the macOS knob the platform admits: none unless it is macOS."""
    if _PLATFORM_KIND[platform_id] != "macos":
        return st.just({})
    floor = _MACOS_TAG_FLOOR[_PLATFORM_ARCH[platform_id]]
    return st.fixed_dictionaries(
        {
            "runs_on_macos": st.none()
            | st.tuples(st.integers(10, 15), st.integers(0, 3)).filter(
                lambda version: version >= floor
            )
        }
    )


def _specs_for(platform_id: str) -> st.SearchStrategy[PlatformSpec]:
    """Build a ``PlatformSpec`` strategy fixed to one platform id.

    Only the knobs that platform admits are drawn, since the others are a
    construction error rather than a spec whose label needs to stay apart.
    """
    return st.builds(
        lambda libc, macos, release, version, ft: PlatformSpec(
            platform_id=platform_id,
            platform_release=release,
            platform_version=version,
            free_threaded=ft,
            **libc,
            **macos,
        ),
        libc=_libc_knobs(platform_id),
        macos=_macos_knobs(platform_id),
        release=rel_strings,
        version=rel_strings,
        ft=st.booleans(),
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
            platforms=tuple(PlatformSpec(p) for p in platforms),
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
        assert [(t.label, t.marker_env) for t in again] == [
            (t.label, t.marker_env) for t in tuples
        ]
        labels = [t.label for t in tuples]
        assert len(set(labels)) == len(labels), labels


class TestLabelInjective:
    """``PlatformSpec.label`` is the documented disambiguator for specs
    sharing a ``platform_id``: distinct specs must render distinct
    labels, otherwise their per-tuple pins silently merge under one
    dict key.
    """

    @given(pair=spec_pairs())
    @PROPERTY_SETTINGS
    def test_label_injective_for_distinct_specs(
        self, pair: tuple[PlatformSpec, PlatformSpec]
    ) -> None:
        """Two distinct same-platform specs never share a label."""
        spec_a, spec_b = pair
        assume(spec_a != spec_b)
        assert spec_a.label != spec_b.label, (
            f"{spec_a!r} and {spec_b!r} collide on label {spec_a.label!r}"
        )


class TestPythonPatchesEnvironment:
    """A ``python_patches`` override pins the tuple's full version;
    ``python_full_version`` and ``implementation_version`` must carry
    the override while ``python_version`` stays the minor.
    """

    @given(
        minor=st.sampled_from(KNOWN_PYTHON_MINORS),
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
            platforms=(PlatformSpec("linux_x86_64"),),
            python_patches={minor: full},
        )
        (tup,) = matrix.expand()
        env = tup.marker_env
        assert env["python_full_version"] == full
        assert env["python_version"] == minor
        assert env["implementation_version"] == full
