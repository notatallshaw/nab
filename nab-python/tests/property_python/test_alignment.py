"""Property tests for cross-target alignment helpers in :mod:`nab_python.resolve`.

A resolve iterates its targets serially with each target's pins threaded
forward as preferences.  The pure helper ``_build_resolver_inputs``
shapes that input: it canonicalises the direct names, drops the
requirements this target's markers exclude, and reports the extras the
root requested.

This file walks the relevant clauses of `PEP 503`_ (canonical names)
and `PEP 508`_ (environment markers) paragraph by paragraph and
adds a property test for each invariant the helper must preserve.

.. _PEP 503: https://peps.python.org/pep-0503/
.. _PEP 508: https://peps.python.org/pep-0508/
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python._vendor.packaging.version import Version
from nab_python.config import NabProjectConfig
from nab_python.lockfile import IndexPin, TargetLock
from nab_python.resolve import (
    ResolveResult,
    TargetResult,
    _build_resolver_inputs,
    build_lock_input,
)
from nab_python.tags import PlatformSpec
from nab_python.target import ResolveTarget
from nab_resolver.errors import ResolutionError

from .strategies import LINUX_TARGET, PROPERTY_SETTINGS

pytestmark = pytest.mark.property


def _fake_target() -> ResolveTarget:
    """Return the minimal linux/3.11 target the property fixtures use."""
    return LINUX_TARGET


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
    canonicalization before the resolver compares them; the keys of
    ``_build_resolver_inputs`` are the direct set the provider is given.

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
        out = _build_resolver_inputs(
            [Requirement(name) for name in names],
            NabProjectConfig(),
            environment=_fake_target().marker_env,
        ).ranges
        assert all(n == n.lower() for n in out)
        if names:
            assert set(out) == {"my-pkg"}


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

    ``_build_resolver_inputs`` evaluates the marker for the target's
    environment and drops requirements whose marker says ``False``;
    otherwise the per-target solve would over-constrain.

    .. _PEP 508: https://peps.python.org/pep-0508/#environment-markers
    """

    @given(reqs=st.lists(requirement_strings(), min_size=0, max_size=5))
    @PROPERTY_SETTINGS
    def test_parse_requirements_drops_non_matching_markers(
        self, reqs: list[str]
    ) -> None:
        """A name survives exactly when one of its requirements matches the env."""
        linux_env = dict(_fake_target().marker_env)
        parsed = [Requirement(text) for text in reqs]

        applies: set[str] = set()
        seen: set[str] = set()
        for req in parsed:
            name = str(canonicalize_name(req.name))
            seen.add(name)
            if req.marker is None or req.marker.evaluate(linux_env):
                applies.add(name)
        excluded = seen - applies

        try:
            out = _build_resolver_inputs(
                parsed,
                NabProjectConfig(),
                environment=linux_env,
            ).ranges
        except ResolutionError:
            # Self-contradictory draws (e.g. ``pkg<0.0``) are rejected
            # by the function; this property tests marker filtering only.
            return

        for name in applies:
            assert name in out
        for name in excluded:
            assert name not in out


@st.composite
def pin_maps(draw: st.DrawFn) -> dict[str, IndexPin]:
    """Draw a non-empty ``{name: IndexPin}`` mapping for a target's lock."""
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
def target_locks(
    draw: st.DrawFn,
) -> list[tuple[ResolveTarget, dict[str, IndexPin]]]:
    """Draw N ``(ResolveTarget, pin_map)`` pairs with unique labels."""
    size = draw(st.integers(min_value=1, max_value=3))
    chosen_platforms = draw(
        st.lists(
            st.sampled_from(_PLATFORM_POOL),
            min_size=size,
            max_size=size,
            unique=True,
        )
    )
    pairs: list[tuple[ResolveTarget, dict[str, IndexPin]]] = []
    for platform in chosen_platforms:
        target = ResolveTarget.for_declared(
            python_version="3.11", spec=PlatformSpec(platform)
        )
        pairs.append((target, draw(pin_maps())))
    return pairs


class TestBuildLockInput:
    """Per-target pins survive the collection into a :class:`LockInput`.

    PEP 751 expects per-environment package entries: callers feed a
    ``ResolveResult`` whose target results each carry a
    :class:`TargetLock`, and the collection must preserve every pin
    under the originating target's label.  A regression that filtered or
    coalesced pins would silently drop installation targets from the
    lockfile.

    Reference: https://peps.python.org/pep-0751/
    """

    @given(pairs=target_locks())
    @PROPERTY_SETTINGS
    def test_lock_input_preserves_per_target_pins(
        self, pairs: list[tuple[ResolveTarget, dict[str, IndexPin]]]
    ) -> None:
        """Every input ``(name, version)`` reappears under the source target's label."""
        target_results = [
            TargetResult(
                target=target,
                success=True,
                pins={name: Version(pin.version) for name, pin in pin_map.items()},
                lock=TargetLock(target=target, pins=pin_map),
            )
            for target, pin_map in pairs
        ]
        result = ResolveResult(
            targets=tuple(target for target, _ in pairs),
            target_results=target_results,
        )
        lock_input = build_lock_input(result)

        for target, pin_map in pairs:
            label = target.label
            assert label in lock_input.targets
            recorded = lock_input.targets[label].pins
            for name, pin in pin_map.items():
                assert name in recorded
                assert recorded[name].version == pin.version
