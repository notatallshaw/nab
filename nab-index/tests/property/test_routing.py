"""Index order, strict package overrides and reuse of a chosen route."""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from nab_index.cache import NullCache
from nab_index.cached_client import CachedAsyncSimpleClient
from nab_index.multi_index import MultiIndexClient
from nab_index.response import HttpxResponse

pytestmark = pytest.mark.property


class RecordingTransport:
    """Serve one index's listing and sidecar and record which index was asked."""

    def __init__(self, index: str, filename: str | None, requests: list[str]) -> None:
        self.index = index
        self.requests = requests
        self.files = (
            [{"filename": filename, "url": f"https://{index}.example/{filename}"}]
            if filename is not None
            else []
        )

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpxResponse:
        self.requests.append(self.index)
        request = httpx.Request("GET", url, headers=headers)
        if url.endswith(".metadata"):
            response = httpx.Response(200, request=request, text=self.index)
        else:
            response = httpx.Response(200, request=request, json={"files": self.files})
        return HttpxResponse(response, response.content)

    async def aclose(self) -> None:
        pass


@st.composite
def index_layouts(draw: st.DrawFn) -> tuple[list[bool], int]:
    """Choose which indexes hold the package and a valid override target."""
    available = draw(st.lists(st.booleans(), min_size=1, max_size=4))
    target = draw(st.integers(min_value=0, max_value=len(available) - 1))
    return available, target


@pytest.mark.parametrize("use_override", [False, True], ids=["ordered", "override"])
@given(
    layout=index_layouts(), stem=st.from_regex(r"[a-z][a-z0-9]{0,7}", fullmatch=True)
)
@example(layout=([True, True], 1), stem="both")
@example(layout=([True, False], 1), stem="missing")
@example(layout=([False, False], 1), stem="absent")
@settings(
    max_examples=1000 if os.environ.get("HYPOTHESIS_PROFILE") == "deep" else 100,
    deadline=None,
)
def test_listing_and_metadata_follow_the_selected_index(
    layout: tuple[list[bool], int], stem: str, *, use_override: bool
) -> None:
    available, target = layout
    order = [f"index{i}" for i in range(len(available))]
    filename = f"{stem}_pkg-1-py3-none-any.whl"
    requests: list[str] = []
    clients = {
        index: CachedAsyncSimpleClient(
            RecordingTransport(index, filename if present else None, requests),
            NullCache(),
            index_url=f"https://{index}.example/simple/",
        )
        for index, present in zip(order, available, strict=True)
    }
    overrides = {f"{stem}__pkg": order[target]} if use_override else {}
    router = MultiIndexClient(clients, order, overrides)

    if use_override:
        chosen = target
        first_lookup = [order[target]]
    elif any(available):
        chosen = available.index(True)
        first_lookup = order[: chosen + 1]
    else:
        chosen = 0
        first_lookup = order

    selected = order[chosen]
    expected_urls = (
        [f"https://{selected}.example/{filename}"] if available[chosen] else []
    )
    requested = f"{stem.upper()}._-PKG"
    repeated = f"{stem}.pkg"

    async def check() -> None:
        async with router:
            files = await router.get_files(requested)
            assert [f.url for f in files] == expected_urls
            assert requests == first_lookup
            assert router.route_for(repeated) == selected

            requests.clear()
            files = await router.get_files(repeated)
            assert [f.url for f in files] == expected_urls
            assert requests == [selected]

            requests.clear()
            text = await router.get_metadata_text(
                f"{stem}-pkg", "1", f"https://files.example/{filename}.metadata"
            )
            assert text == selected
            assert requests == [selected]

    asyncio.run(check())
