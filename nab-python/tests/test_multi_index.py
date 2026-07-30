"""Tests for nab_index.multi_index.MultiIndexClient."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypeVar

import pytest
from packaging.utils import canonicalize_name

from nab_index._naming import canonical as _normalise_name
from nab_index.cache import OfflineError, OnDiskCache
from nab_index.cached_client import CachedAsyncSimpleClient
from nab_index.client import SdistFile, WheelFile
from nab_index.lazy_wheel import RangeMetadataResult, RangeOutcome
from nab_index.local_index import LocalIndexClient
from nab_index.multi_index import IndexConfig, MultiIndexClient

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from pathlib import Path
    from typing import Any, NoReturn

_T = TypeVar("_T")


def run(coro: Coroutine[Any, Any, _T]) -> _T:
    return asyncio.run(coro)


def _wheel(name: str, version: str = "1.0") -> WheelFile:
    return WheelFile(
        filename=f"{name}-{version}-py3-none-any.whl",
        url=f"https://example.com/{name}-{version}-py3-none-any.whl",
        version=version,
        requires_python=None,
        has_metadata=False,
        upload_time=None,
    )


class FakeClient:
    """Stand-in for CachedAsyncSimpleClient / LocalIndexClient."""

    def __init__(self, listing: dict[str, list[WheelFile | SdistFile]]) -> None:
        self.listing = listing
        self.unreadable: set[str] = set()
        self.get_files_calls: list[str] = []
        self.metadata_calls: list[tuple[str, str, str]] = []
        self.sdist_calls: list[tuple[str, str, str]] = []
        self.range_calls: list[tuple[str, str, str]] = []
        self.close_count = 0

    @property
    def closed(self) -> bool:
        return self.close_count > 0

    async def get_files(self, package: str) -> list[WheelFile | SdistFile]:
        self.get_files_calls.append(package)
        return list(self.listing.get(_normalise_name(package), []))

    def served_unreadable_only(self, package: str) -> bool:
        return package in self.unreadable

    async def get_metadata_text(
        self,
        package: str,
        version: str,
        metadata_url: str,
        metadata_hash: tuple[str, str] | None = None,
    ) -> str:
        self.metadata_calls.append((package, version, metadata_url))
        return f"meta:{package}:{version}"

    async def get_sdist_files(
        self,
        package: str,
        version: str,
        sdist_url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> tuple[str | None, str | None]:
        self.sdist_calls.append((package, version, sdist_url))
        return (None, None)

    async def get_sdist_archive(
        self,
        package: str,
        version: str,
        sdist_url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> bytes:
        return b""

    async def get_range_metadata(
        self,
        package: str,
        version: str,
        wheel_url: str,
        canonical_name: str,
        wheel_hashes: tuple[tuple[str, str], ...] = (),
    ) -> RangeMetadataResult:
        self.range_calls.append((package, version, wheel_url))
        return RangeMetadataResult(f"range:{package}:{version}", RangeOutcome.PARTIAL)

    async def aclose(self) -> None:
        self.close_count += 1


class _NoNetworkTransport:
    """Transport that fails the test on any request."""

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> NoReturn:
        msg = f"unexpected network call: {url}"
        raise AssertionError(msg)

    async def aclose(self) -> None:
        return None


def _offline_client(tmp_path: Path, name: str) -> CachedAsyncSimpleClient:
    """Real cached client in offline mode with a cold on-disk cache."""
    url = f"https://{name}.example/simple/"
    cache = OnDiskCache(tmp_path / name, url)
    return CachedAsyncSimpleClient(_NoNetworkTransport(), cache, url, offline=True)


class TestIndexConfig:
    def test_construction(self) -> None:
        cfg = IndexConfig(name="pypi", url="https://pypi.org/simple/")
        assert cfg.name == "pypi"
        assert cfg.url == "https://pypi.org/simple/"


class TestNormaliseName:
    def test_canonical_dashes(self) -> None:
        assert _normalise_name("Foo_Bar.Baz") == "foo-bar-baz"

    def test_collapses_consecutive_separators(self) -> None:
        assert _normalise_name("foo___bar") == "foo-bar"


class TestPresenceBased:
    def test_first_index_hit_short_circuits(self) -> None:
        first = FakeClient({"foo": [_wheel("foo")]})
        second = FakeClient({"foo": [_wheel("foo", "2.0")]})
        client = MultiIndexClient(
            {"a": first, "b": second},
            ["a", "b"],
            {},
        )
        result = run(client.get_files("foo"))
        assert len(result) == 1
        assert result[0].version == "1.0"
        # Second was not consulted
        assert second.get_files_calls == []

    def test_first_miss_falls_through(self) -> None:
        first = FakeClient({})
        second = FakeClient({"foo": [_wheel("foo")]})
        client = MultiIndexClient(
            {"a": first, "b": second},
            ["a", "b"],
            {},
        )
        result = run(client.get_files("foo"))
        assert len(result) == 1
        assert first.get_files_calls == ["foo"]
        assert second.get_files_calls == ["foo"]

    def test_all_miss_returns_empty(self) -> None:
        first = FakeClient({})
        second = FakeClient({})
        client = MultiIndexClient(
            {"a": first, "b": second},
            ["a", "b"],
            {},
        )
        assert run(client.get_files("foo")) == []

    def test_unreadable_only_reported_from_any_walked_index(self) -> None:
        """An empty walk routes to the first index, so every client is asked."""
        first = FakeClient({})
        second = FakeClient({})
        second.unreadable.add("foo")
        client = MultiIndexClient(
            {"a": first, "b": second},
            ["a", "b"],
            {},
        )
        assert run(client.get_files("foo")) == []
        assert client.served_unreadable_only("foo")
        assert not client.served_unreadable_only("bar")

    def test_route_cache_subsequent_calls(self) -> None:
        first = FakeClient({})
        second = FakeClient({"foo": [_wheel("foo")]})
        client = MultiIndexClient(
            {"a": first, "b": second},
            ["a", "b"],
            {},
        )
        run(client.get_files("foo"))
        # Second call hits the cached route
        run(client.get_files("foo"))
        assert first.get_files_calls == ["foo"]
        assert second.get_files_calls == ["foo", "foo"]


class TestOfflinePresenceWalk:
    """A cold offline index must not mask later indexes in the walk."""

    def test_cold_offline_index_falls_through_to_local(self, tmp_path: Path) -> None:
        wheelhouse = tmp_path / "wheelhouse"
        wheelhouse.mkdir()
        (wheelhouse / "foo-1.0-py3-none-any.whl").write_bytes(b"")

        client = MultiIndexClient(
            {
                "pypi": _offline_client(tmp_path, "pypi"),
                "local": LocalIndexClient(wheelhouse.as_uri()),
            },
            ["pypi", "local"],
            {},
        )

        files = run(client.get_files("foo"))
        assert [f.filename for f in files] == ["foo-1.0-py3-none-any.whl"]
        assert client.route_for("foo") == "local"

    def test_cold_offline_all_indexes_raises(self, tmp_path: Path) -> None:
        client = MultiIndexClient(
            {
                "a": _offline_client(tmp_path, "a"),
                "b": _offline_client(tmp_path, "b"),
            },
            ["a", "b"],
            {},
        )

        with pytest.raises(OfflineError):
            run(client.get_files("foo"))
        assert client.route_for("foo") == "a"

    def test_answering_index_does_not_mask_a_cold_one(self, tmp_path: Path) -> None:
        """One index answering absent does not settle it while another is cold."""
        client = MultiIndexClient(
            {"answers": FakeClient({}), "cold": _offline_client(tmp_path, "cold")},
            ["answers", "cold"],
            {},
        )

        with pytest.raises(OfflineError):
            run(client.get_files("foo"))

    def test_override_pin_to_cold_offline_index_raises(self, tmp_path: Path) -> None:
        wheelhouse = tmp_path / "wheelhouse"
        wheelhouse.mkdir()
        (wheelhouse / "foo-1.0-py3-none-any.whl").write_bytes(b"")

        client = MultiIndexClient(
            {
                "pypi": _offline_client(tmp_path, "pypi"),
                "local": LocalIndexClient(wheelhouse.as_uri()),
            },
            ["pypi", "local"],
            {"foo": "pypi"},
        )

        with pytest.raises(OfflineError):
            run(client.get_files("foo"))


class TestOverrides:
    def test_override_wins(self) -> None:
        pypi = FakeClient({"foo": [_wheel("foo", "1.0")]})
        torch = FakeClient({"foo": [_wheel("foo", "9.9")]})
        client = MultiIndexClient(
            {"pypi": pypi, "torch": torch},
            ["pypi", "torch"],
            {"foo": "torch"},
        )
        result = run(client.get_files("foo"))
        assert result[0].version == "9.9"
        assert pypi.get_files_calls == []
        assert torch.get_files_calls == ["foo"]

    def test_override_strict_no_fall_through(self) -> None:
        pypi = FakeClient({"foo": [_wheel("foo", "1.0")]})
        torch = FakeClient({})  # missing the package
        client = MultiIndexClient(
            {"pypi": pypi, "torch": torch},
            ["pypi", "torch"],
            {"foo": "torch"},
        )
        # Strict pin - nothing returned even though pypi has it
        assert run(client.get_files("foo")) == []
        assert pypi.get_files_calls == []

    def test_canonical_name_match_in_overrides(self) -> None:
        pypi = FakeClient({})
        other = FakeClient({"foo-bar": [_wheel("foo-bar")]})
        client = MultiIndexClient(
            {"pypi": pypi, "other": other},
            ["pypi", "other"],
            {"FOO_BAR": "other"},
        )
        result = run(client.get_files("Foo.Bar"))
        assert len(result) == 1


class TestMetadataRouting:
    def test_metadata_goes_to_routed_client(self) -> None:
        first = FakeClient({})
        second = FakeClient({"foo": [_wheel("foo")]})
        client = MultiIndexClient(
            {"a": first, "b": second},
            ["a", "b"],
            {},
        )
        run(client.get_files("foo"))
        text = run(client.get_metadata_text("foo", "1.0", "https://x/m.metadata"))
        assert text == "meta:foo:1.0"
        assert second.metadata_calls == [("foo", "1.0", "https://x/m.metadata")]
        assert first.metadata_calls == []

    def test_sdist_goes_to_routed_client(self) -> None:
        first = FakeClient({})
        second = FakeClient({"foo": [_wheel("foo")]})
        client = MultiIndexClient(
            {"a": first, "b": second},
            ["a", "b"],
            {},
        )
        run(client.get_files("foo"))
        run(client.get_sdist_files("foo", "1.0", "https://x/s.tar.gz"))
        assert second.sdist_calls == [("foo", "1.0", "https://x/s.tar.gz")]

    def test_metadata_without_prior_get_files_uses_first(self) -> None:
        first = FakeClient({})
        second = FakeClient({})
        client = MultiIndexClient(
            {"a": first, "b": second},
            ["a", "b"],
            {},
        )
        run(client.get_metadata_text("foo", "1.0", "https://x/m.metadata"))
        assert first.metadata_calls == [("foo", "1.0", "https://x/m.metadata")]

    def test_range_metadata_goes_to_routed_client(self) -> None:
        first = FakeClient({})
        second = FakeClient({"foo": [_wheel("foo")]})
        client = MultiIndexClient(
            {"a": first, "b": second},
            ["a", "b"],
            {},
        )
        run(client.get_files("foo"))
        result = run(
            client.get_range_metadata(
                "foo",
                "1.0",
                "https://x/foo-1.0-py3-none-any.whl",
                canonicalize_name("foo"),
            )
        )
        assert result.text == "range:foo:1.0"
        assert result.outcome is RangeOutcome.PARTIAL
        assert second.range_calls == [
            ("foo", "1.0", "https://x/foo-1.0-py3-none-any.whl")
        ]
        assert first.range_calls == []

    def test_range_metadata_without_prior_get_files_uses_first(self) -> None:
        first = FakeClient({})
        second = FakeClient({})
        client = MultiIndexClient(
            {"a": first, "b": second},
            ["a", "b"],
            {},
        )
        run(
            client.get_range_metadata(
                "foo", "1.0", "https://x/foo.whl", canonicalize_name("foo")
            )
        )
        assert first.range_calls == [("foo", "1.0", "https://x/foo.whl")]


class TestAClose:
    def test_closes_all_owned_clients(self) -> None:
        a = FakeClient({})
        b = FakeClient({})
        c = FakeClient({})
        client = MultiIndexClient(
            {"a": a, "b": b, "c": c},
            ["a", "b", "c"],
            {},
        )
        run(client.aclose())
        assert a.closed
        assert b.closed
        assert c.closed

    def test_dedup_repeat_close(self) -> None:
        # A client shared under two names must close exactly once.
        shared = FakeClient({})
        client = MultiIndexClient(
            {"a": shared, "alias": shared},
            ["a"],
            {},
        )
        run(client.aclose())
        assert shared.close_count == 1

    def test_async_with(self) -> None:
        a = FakeClient({})
        client = MultiIndexClient({"a": a}, ["a"], {})

        async def go() -> bool:
            async with client as ctx:
                return ctx is client

        assert run(go())
        assert a.closed


class TestValidation:
    def test_empty_order_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            MultiIndexClient({"a": FakeClient({})}, [], {})

    def test_unknown_order_name_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown index names"):
            MultiIndexClient({"a": FakeClient({})}, ["nonexistent"], {})

    def test_unknown_override_target_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown index names"):
            MultiIndexClient(
                {"a": FakeClient({})},
                ["a"],
                {"foo": "no-such-index"},
            )
