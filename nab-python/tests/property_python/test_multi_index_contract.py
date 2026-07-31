"""Property tests for the :mod:`nab_index.multi_index` routing contract.

Contract (module docstring): walk the ordered index list left-to-right,
stop at the first index whose listing is non-empty; an override pins a
package to exactly one named index.  Listings from different indexes are
never mixed.  ``route_for`` reports the serving index after ``get_files``,
and follow-up metadata calls hit that same index whatever PEP 503
spelling they use.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypeVar

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_index._naming import canonical
from nab_index.client import SdistFile, WheelFile
from nab_index.multi_index import MultiIndexClient

from .strategies import PROPERTY_SETTINGS

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

pytestmark = pytest.mark.property

_T = TypeVar("_T")

Universe = tuple[MultiIndexClient, "list[Stub]", "dict[str, str]"]


def run(coro: Coroutine[Any, Any, _T]) -> _T:
    """Synchronously run an awaitable inside a property test."""
    return asyncio.run(coro)


def _wheel(name: str, idx: str) -> WheelFile:
    """Build a wheel whose URL host identifies the serving index."""
    return WheelFile(
        filename=f"{name}-1.0-py3-none-any.whl",
        url=f"https://{idx}.example/{name}-1.0-py3-none-any.whl",
        version="1.0",
        requires_python=None,
        has_metadata=False,
        upload_time=None,
    )


class Stub:
    """Index client serving one wheel per hosted package, recording calls."""

    def __init__(self, idx_name: str, has: set[str]) -> None:
        self.idx_name = idx_name
        self.has = has
        self.get_files_calls: list[str] = []
        self.metadata_calls: list[str] = []

    async def get_files(self, package: str) -> list[WheelFile | SdistFile]:
        self.get_files_calls.append(package)
        if canonical(package) in self.has:
            return [_wheel(canonical(package), self.idx_name)]
        return []

    def served_unreadable_only(self, package: str) -> bool:
        return False

    async def get_metadata_text(
        self,
        package: str,
        version: str,
        metadata_url: str,
        metadata_hash: tuple[str, str] | None = None,
    ) -> str:
        del version, metadata_url, metadata_hash
        self.metadata_calls.append(package)
        return f"served-by:{self.idx_name}"

    async def get_sdist_files(
        self, package: str, version: str, sdist_url: str
    ) -> tuple[str | None, str | None]:
        del package, version, sdist_url
        return (None, None)

    async def get_sdist_archive(
        self, package: str, version: str, sdist_url: str
    ) -> bytes:
        del package, version, sdist_url
        return b""

    async def aclose(self) -> None:
        pass


pkg_names = st.from_regex(r"[a-z]{1,4}", fullmatch=True)
spellings = st.sampled_from([str.upper, str.title, str, lambda s: s.replace("a", "A")])


@st.composite
def universes(draw: st.DrawFn) -> Universe:
    """Router over 1-4 stub indexes with random holdings and overrides."""
    n = draw(st.integers(min_value=1, max_value=4))
    stubs = [Stub(f"idx{i}", draw(st.sets(pkg_names, max_size=4))) for i in range(n)]
    clients = {s.idx_name: s for s in stubs}
    order = [s.idx_name for s in stubs]
    overrides = draw(st.dictionaries(pkg_names, st.sampled_from(order), max_size=2))
    return MultiIndexClient(clients, order, overrides), stubs, overrides


@given(data=universes(), package=pkg_names)
@PROPERTY_SETTINGS
def test_first_nonempty_index_wins_and_no_mixing(data: Universe, package: str) -> None:
    """The first non-empty index serves the listing; later ones stay unasked."""
    router, stubs, overrides = data
    can = canonical(package)
    files = run(router.get_files(package))

    if can in overrides:
        target = overrides[can]
        winner = next(s for s in stubs if s.idx_name == target)
        expected = [_wheel(can, winner.idx_name)] if can in winner.has else []
        assert files == expected
        assert router.route_for(package) == target
        return

    expected_winner = None
    for s in stubs:
        if can in s.has:
            expected_winner = s
            break

    if expected_winner is None:
        assert files == []
        # All indexes consulted, attribution falls back to the first index.
        assert all(s.get_files_calls == [package] for s in stubs)
        assert router.route_for(package) == stubs[0].idx_name
    else:
        assert files == [_wheel(can, expected_winner.idx_name)]
        assert router.route_for(package) == expected_winner.idx_name
        seen_winner = False
        for s in stubs:
            if s is expected_winner:
                seen_winner = True
                assert s.get_files_calls == [package]
            elif not seen_winner:
                assert s.get_files_calls == [package]
            else:
                assert s.get_files_calls == []

    # No mixing: every returned file came from exactly one index.
    hosts = {f.url.split("/")[2] for f in files}
    assert len(hosts) <= 1


@given(data=universes(), package=pkg_names, respell=spellings)
@PROPERTY_SETTINGS
def test_metadata_follows_listing_route(
    data: Universe, package: str, respell: Callable[[str], str]
) -> None:
    """Metadata after ``get_files`` hits the index that served the listing."""
    router, _stubs, _overrides = data
    run(router.get_files(package))
    served = router.route_for(package)
    text = run(router.get_metadata_text(respell(package), "1.0", "https://x/m"))
    assert text == f"served-by:{served}"


@given(data=universes(), package=pkg_names, respell=spellings)
@PROPERTY_SETTINGS
def test_route_stable_across_spellings(
    data: Universe, package: str, respell: Callable[[str], str]
) -> None:
    """A second ``get_files`` under another PEP 503 spelling keeps the route."""
    router, _stubs, _overrides = data
    first = run(router.get_files(package))
    route_first = router.route_for(package)
    second = run(router.get_files(respell(package)))
    assert router.route_for(respell(package)) == route_first
    assert [f.filename for f in second] == [f.filename for f in first]
