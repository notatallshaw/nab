"""Property tests for the extras-handling implementation in :mod:`nab_python.provider`.

`PEP 685`_ specifies how extra names are normalized for comparison
and how distributions express dependencies that are conditional on an
extra being requested.  `PEP 503`_ defines canonical-name
normalization rules that PEP 685 reuses.

Each section of this file walks the relevant paragraph of those PEPs
and adds property tests for the invariant.

.. _PEP 503: https://peps.python.org/pep-0503/#normalized-names
.. _PEP 685: https://peps.python.org/pep-0685/
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_index.client import WheelFile
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python._vendor.packaging.version import Version
from nab_python.provider import (
    ExtrasMode,
    MissingExtraError,
    Provider,
    join_extra,
    split_extra,
)
from nab_python.target import ResolveTarget

from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

EXTRA_NAMES = ["security", "dev", "test", "docs", "all"]


@st.composite
def extras_metadata(draw: st.DrawFn) -> str:
    """Generate METADATA text with random extras and base/per-extra deps."""
    num_extras = draw(st.integers(min_value=0, max_value=3))
    extras = draw(
        st.lists(
            st.sampled_from(EXTRA_NAMES),
            min_size=num_extras,
            max_size=num_extras,
            unique=True,
        )
    )
    num_base_deps = draw(st.integers(min_value=0, max_value=2))
    base_dep_names = [f"basedep{i}" for i in range(num_base_deps)]

    lines = [
        "Metadata-Version: 2.1",
        "Name: testpkg",
        "Version: 1.0",
    ]
    for extra in extras:
        lines.append(f"Provides-Extra: {extra}")
    for dep in base_dep_names:
        has_dep_extra = draw(st.booleans())
        if has_dep_extra:
            lines.append(f"Requires-Dist: {dep}[feat]>=1.0")
        else:
            lines.append(f"Requires-Dist: {dep}>=1.0")
    for extra in extras:
        num_extra_deps = draw(st.integers(min_value=0, max_value=2))
        for i in range(num_extra_deps):
            has_dep_extra = draw(st.booleans())
            if has_dep_extra:
                lines.append(
                    f'Requires-Dist: {extra}-dep{i}[feat]>=1.0; extra == "{extra}"'
                )
            else:
                lines.append(f'Requires-Dist: {extra}-dep{i}>=1.0; extra == "{extra}"')

    return "\n".join(lines)


def _make_extras_provider(
    metadata_text: str,
    extras_mode: ExtrasMode = ExtrasMode.WARN,
) -> Provider:
    """Build a ``Provider`` whose only entry is ``testpkg-1.0``."""
    wheel = WheelFile(
        filename="testpkg-1.0-py3-none-any.whl",
        url="https://example.com/testpkg-1.0-py3-none-any.whl",
        version="1.0",
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )
    coordinator = make_coordinator([wheel], package="testpkg")

    # Pre-store metadata so the resolver short-circuits on the
    # has_metadata cache hit before the side effects fire.
    coordinator.index.store_metadata("testpkg", "1.0", metadata_text)
    return Provider(
        coordinator,
        target=ResolveTarget.for_host_python("3.12.0"),
        extras_mode=extras_mode,
    )


class TestQuoteExtraNameNormalization:
    """PEP 685, § Specification:

    > "When comparing extra names, tools MUST normalize the names
    > being compared using the semantics outlined in PEP 503 for
    > names: ``re.sub(r"[-_.]+", "-", name).lower()``"

    The module's ``join_extra``/``split_extra`` round-trip must apply
    that normalization, so that ``Security``, ``security``,
    ``se_curity``, and ``se.curity`` all yield the same proxy key.

    Reference: https://peps.python.org/pep-0685/#specification
    """

    @given(extra=st.sampled_from(["Security", "SECURITY", "security", "se_curity"]))
    @PROPERTY_SETTINGS
    def test_split_join_normalizes_case(self, extra: str) -> None:
        """``split_extra(join_extra(b, e))`` returns the canonicalised form."""
        key = join_extra("pkg", extra)
        _, parsed = split_extra(key)
        assert parsed is not None
        assert parsed == parsed.lower().replace("_", "-")

    @given(
        a=st.sampled_from(["Security", "SECURITY", "security"]),
        b=st.sampled_from(["Security", "SECURITY", "security"]),
    )
    @PROPERTY_SETTINGS
    def test_case_variants_produce_same_key(self, a: str, b: str) -> None:
        """Different cases of the same extra name yield equal proxy keys."""
        assert join_extra("pkg", a) == join_extra("pkg", b)

    @given(
        a=st.sampled_from(["dev_test", "dev-test", "dev.test"]),
        b=st.sampled_from(["dev_test", "dev-test", "dev.test"]),
    )
    @PROPERTY_SETTINGS
    def test_separator_variants_produce_same_key(self, a: str, b: str) -> None:
        """Hyphens, underscores, and dots are all normalized to hyphens."""
        assert join_extra("pkg", a) == join_extra("pkg", b)


class TestExtraNameRoundtrip:
    """PEP 685 mandates that two extras that normalize to the same
    name are equivalent.  The round-trip
    ``split_extra(join_extra(name, extra))`` must preserve the
    package name and produce the canonicalised extra.

    Reference: https://peps.python.org/pep-0685/#specification
    """

    @given(
        base=st.sampled_from(["mypkg", "my-pkg", "MY-PKG", "my_pkg"]),
        extra=st.sampled_from(["Dev_Test", "dev-test", "DEV.TEST"]),
    )
    @PROPERTY_SETTINGS
    def test_split_roundtrip(self, base: str, extra: str) -> None:
        """``split_extra(join_extra(b, e))`` returns ``(b, normalized_e)``."""
        parsed_base, parsed_extra = split_extra(join_extra(base, extra))
        assert parsed_base == base
        assert parsed_extra is not None
        assert parsed_extra == parsed_extra.lower().replace("_", "-")


class TestExtrasNeverCrashOnArbitrary:
    """The implementation must never crash when asked for an extra
    that the metadata may or may not declare.  Missing extras
    surface as :class:`MissingExtraError` (caught via
    ``ExtrasMode``).
    """

    @given(metadata=extras_metadata())
    @PROPERTY_SETTINGS
    def test_extras_never_crash(self, metadata: str) -> None:
        """``get_dependencies(name[extra], v)`` never crashes."""
        provider = _make_extras_provider(metadata)
        for extra in EXTRA_NAMES:
            try:
                provider.get_dependencies(f"testpkg[{extra}]", Version("1.0"))
            except MissingExtraError:
                pass


class TestExtrasIncludeBase:
    """An extra ``X`` of package ``P`` is, by construction, an
    extension of ``P``.  Selecting ``P[X]`` therefore always pulls in
    ``P`` itself.

    Required for the proxy-package construction in nab to correctly
    model the user-facing meaning of ``Provides-Extra`` /
    ``Requires-Dist; extra == "X"`` (`Core Metadata § Provides-Extra`_).

    .. _Core Metadata § Provides-Extra:
       https://packaging.python.org/en/latest/specifications/core-metadata/#provides-extra-multiple-use
    """

    @given(metadata=extras_metadata())
    @PROPERTY_SETTINGS
    def test_extras_always_include_base_dep(self, metadata: str) -> None:
        """Extras deps include ``testpkg`` itself when non-empty."""
        provider = _make_extras_provider(metadata)
        for extra in EXTRA_NAMES:
            deps = provider.get_dependencies(f"testpkg[{extra}]", Version("1.0"))
            if deps:
                assert "testpkg" in deps


class TestExtrasDoNotDuplicateBase:
    """An extra's deps must not also appear in the base package's deps.

    Otherwise the resolver would face conflicting constraints from a
    single ``Requires-Dist`` line, and the lockfile would carry
    duplicate entries.
    """

    @given(metadata=extras_metadata())
    @PROPERTY_SETTINGS
    def test_extras_deps_disjoint_from_base(self, metadata: str) -> None:
        """No base-name dep appears in both base and an extras bucket."""
        provider = _make_extras_provider(metadata)
        base_deps = provider.get_dependencies("testpkg", Version("1.0"))
        base_names = {split_extra(d)[0] for d in base_deps}
        for extra in EXTRA_NAMES:
            extra_deps = provider.get_dependencies(f"testpkg[{extra}]", Version("1.0"))
            for dep_name in extra_deps:
                dep_base, dep_extra = split_extra(dep_name)
                if dep_extra is None and dep_base != "testpkg":
                    assert dep_base not in base_names


class TestExtrasIdempotent:
    """Calling ``get_dependencies`` twice with the same arguments
    returns the same result.

    The Provider caches the parsed metadata internally; a stateful
    implementation that returns different results on the second call
    would break the resolver's correctness assumptions.
    """

    @given(metadata=extras_metadata())
    @PROPERTY_SETTINGS
    def test_extras_idempotent(self, metadata: str) -> None:
        """``get_dependencies(name[extra], v)`` is idempotent."""
        provider = _make_extras_provider(metadata)
        for extra in EXTRA_NAMES:
            deps1 = provider.get_dependencies(f"testpkg[{extra}]", Version("1.0"))
            deps2 = provider.get_dependencies(f"testpkg[{extra}]", Version("1.0"))
            assert deps1 == deps2


class TestTransitiveExtrasCreateProxies:
    """A dep with an extra (``Requires-Dist: foo[ex]``) must yield a
    proxy entry alongside the base ``foo`` dep, so that the resolver
    can resolve ``foo[ex]``'s dependencies independently of plain
    ``foo``'s.

    The provider must not introduce a proxy without also pulling in
    the base; otherwise the proxy would never resolve.
    """

    @given(metadata=extras_metadata())
    @PROPERTY_SETTINGS
    def test_transitive_extras_create_proxies(self, metadata: str) -> None:
        """Every proxy in base deps has its base also present."""
        provider = _make_extras_provider(metadata)
        base_deps = provider.get_dependencies("testpkg", Version("1.0"))
        for dep_name in base_deps:
            dep_base, dep_extra = split_extra(dep_name)
            if dep_extra is not None:
                assert dep_base in base_deps, (
                    f"Proxy {dep_name} without base {dep_base} in deps"
                )

    @given(metadata=extras_metadata())
    @PROPERTY_SETTINGS
    def test_extra_transitive_extras_create_proxies(self, metadata: str) -> None:
        """Extra-gated deps with extras create proxy packages."""
        provider = _make_extras_provider(metadata)
        for extra in EXTRA_NAMES:
            deps = provider.get_dependencies(f"testpkg[{extra}]", Version("1.0"))
            for dep_name in deps:
                dep_base, dep_extra = split_extra(dep_name)
                if dep_extra is not None and dep_base != "testpkg":
                    base_deps = provider.get_dependencies("testpkg", Version("1.0"))
                    assert dep_base in deps or dep_base in base_deps, (
                        f"Proxy {dep_name} without base {dep_base}"
                    )
