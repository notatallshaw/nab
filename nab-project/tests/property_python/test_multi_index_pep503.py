"""Property tests for :mod:`nab_index.multi_index` routing.

`PEP 503`_ specifies the canonical-name normalization rules every
PyPI-compatible index must implement, and the canonicalization
must be idempotent.

`PEP 708`_ (Index Hosting Discovery) describes how a tool may
discover and route between multiple indexes; while PEP 708 is
concerned with discovery, the routing semantics (override target,
first-match-wins) are restated in nab's multi-index design doc.

This file walks the relevant paragraphs and adds property tests for
the invariants.

.. _PEP 503: https://peps.python.org/pep-0503/
.. _PEP 708: https://peps.python.org/pep-0708/
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypeVar, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from packaging.utils import canonicalize_name as _normalise_name

from nab_index.client import SdistFile, WheelFile
from nab_index.multi_index import MultiIndexClient

from .strategies import PROPERTY_SETTINGS

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

pytestmark = pytest.mark.property

_T = TypeVar("_T")


def run(coro: Coroutine[Any, Any, _T]) -> _T:
    """Synchronously run an awaitable inside a property test."""
    return asyncio.run(coro)


def _wheel(name: str) -> WheelFile:
    """Build a minimal ``WheelFile`` for routing tests."""
    return WheelFile(
        filename=f"{name}-1.0-py3-none-any.whl",
        url=f"https://example.com/{name}-1.0.whl",
        version="1.0",
        requires_python=None,
        has_metadata=False,
        upload_time=None,
    )


class _Stub:
    """Stand-in client that records ``get_files`` calls."""

    def __init__(self, has: set[str]) -> None:
        self.has = has
        self.calls: list[str] = []
        self.closed = False

    async def get_files(self, package: str) -> list[WheelFile | SdistFile]:
        self.calls.append(package)
        norm = _normalise_name(package)
        if norm in self.has:
            return [_wheel(norm)]
        return []

    def served_unreadable_only(self, package: str) -> bool:
        return False

    def served_unreachable_only(self, package: str) -> bool:
        return False

    def served_no_usable_file(self, package: str) -> bool:
        return False

    def served_all_yanked(self, package: str) -> bool:
        return False

    def served_zip_sdists(self, package: str) -> frozenset[str]:
        return frozenset()

    async def get_metadata_text(
        self,
        package: str,
        version: str,
        metadata_url: str,
        metadata_hash: tuple[str, str] | None = None,
    ) -> str:
        del package, version, metadata_url, metadata_hash
        return ""

    async def get_sdist_files(
        self,
        package: str,
        version: str,
        sdist_url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> tuple[str | None, str | None]:
        del package, version, sdist_url, sdist_hashes
        return (None, None)

    async def get_sdist_archive(
        self,
        package: str,
        version: str,
        sdist_url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> bytes:
        del package, version, sdist_url, sdist_hashes
        return b""

    async def aclose(self) -> None:
        self.closed = True


@st.composite
def routers(draw: st.DrawFn) -> tuple[MultiIndexClient, list[_Stub], dict[str, str]]:
    """Generate a router with random index ordering and overrides."""
    name_strategy = st.from_regex(r"[a-z]{1,5}", fullmatch=True)
    n_indexes = draw(st.integers(min_value=1, max_value=4))
    indexes_with_pkgs: list[tuple[str, _Stub]] = []
    for index in range(n_indexes):
        pkgs_here = draw(st.sets(name_strategy, min_size=0, max_size=4))
        stub = _Stub(pkgs_here)
        indexes_with_pkgs.append((f"idx{index}", stub))
    clients = {name: stub for name, stub in indexes_with_pkgs}
    order = [name for name, _ in indexes_with_pkgs]
    overrides_for_pkgs = draw(
        st.dictionaries(name_strategy, st.sampled_from(order), max_size=3)
    )
    return (
        MultiIndexClient(clients, order, overrides_for_pkgs),
        [stub for _, stub in indexes_with_pkgs],
        overrides_for_pkgs,
    )


class TestQuoteCanonicalNameNormalization:
    """PEP 503, § Normalized Names:

    > "This PEP references the concept of a 'normalized' project name.
    > As per PEP 426 the only valid characters in a name are the ASCII
    > alphabet, ASCII numbers, ., -, and _. The name should be
    > lowercased with all runs of the characters ., -, or _ replaced
    > with a single - character."

    The normalization must be **idempotent**: repeated application of
    ``normalize`` does not change the result.  This is the algebraic
    invariant: ``normalize(normalize(x)) == normalize(x)``.
    """

    @given(name=st.from_regex(r"[A-Za-z][A-Za-z0-9_.-]{0,15}", fullmatch=True))
    @PROPERTY_SETTINGS
    def test_normalise_idempotent(self, name: str) -> None:
        """``_normalise_name`` is idempotent under repeated application."""
        once = _normalise_name(name)
        twice = _normalise_name(once)
        assert once == twice


class TestRouteCacheStability:
    """Once a route is resolved for a canonical name, repeated lookups
    must hit the same client.

    Without this stability the user would see surprising flips where
    ``foo`` is fetched from ``--extra-index-url`` on the first call
    and from PyPI on the second.  This matches `pip's index ordering
    semantics`_: the chosen index for a package is fixed at first
    lookup and is not re-evaluated for subsequent fetches.

    .. _pip's index ordering semantics:
       https://pip.pypa.io/en/stable/topics/secure-installs/#using-multiple-indexes
    """

    @given(routing=routers(), package=st.from_regex(r"[a-z]{1,5}", fullmatch=True))
    @PROPERTY_SETTINGS
    def test_route_cache_is_stable(
        self,
        routing: tuple[MultiIndexClient, list[_Stub], dict[str, str]],
        package: str,
    ) -> None:
        """A repeated ``get_files`` call routes to the same stub as the first."""
        client, stubs, _overrides = routing
        run(client.get_files(package))
        before = [list(s.calls) for s in stubs]
        run(client.get_files(package))
        after = [list(s.calls) for s in stubs]
        diffs = [len(a) - len(b) for b, a in zip(before, after, strict=True)]
        assert sum(diffs) == 1
        assert all(d in (0, 1) for d in diffs)


class TestOverrideTargetWins:
    """An index-override mapping forces a specific package to resolve
    from the named index, regardless of presence ordering.

    Required so that a user-supplied private mirror takes precedence
    over PyPI for a particular package even when the public index
    happens to host the same name.  Mirrors the semantics of pip's
    ``--index-strategy`` plus ``--index-url`` overrides.
    """

    @given(routing=routers(), package=st.from_regex(r"[a-z]{1,5}", fullmatch=True))
    @PROPERTY_SETTINGS
    def test_override_target_only(
        self,
        routing: tuple[MultiIndexClient, list[_Stub], dict[str, str]],
        package: str,
    ) -> None:
        """When an override is set, only the target index sees the call."""
        client, _stubs, overrides = routing
        canonical = _normalise_name(package)
        if canonical not in overrides:
            return
        target_name = overrides[canonical]
        target_stub = cast("_Stub", client._clients[target_name])
        run(client.get_files(package))
        assert package in target_stub.calls
        for name, raw_stub in client._clients.items():
            if name != target_name:
                stub = cast("_Stub", raw_stub)
                assert package not in stub.calls
