"""Prefetch windows belong to an individual provider or fetch operation."""

from __future__ import annotations

import pytest

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.version import Version
from nab_provider.fetch_port import PrefetchSettings
from nab_provider.provider import Provider
from nab_provider.testing import make_coordinator, make_wheel


def test_unspecified_windows_leave_defaults_to_the_consumer() -> None:
    settings = PrefetchSettings()

    assert settings.metadata_versions is None
    assert settings.lookahead_versions is None


@pytest.mark.parametrize("count", [0, 1, 16, 64])
def test_explicit_windows_are_preserved(count: int) -> None:
    settings = PrefetchSettings(metadata_versions=count, lookahead_versions=count)

    assert settings.metadata_versions == count
    assert settings.lookahead_versions == count


@pytest.mark.parametrize("field", ["metadata_versions", "lookahead_versions"])
def test_negative_windows_are_refused(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        PrefetchSettings(**{field: -1})


@pytest.mark.parametrize("window", [0, 1, 3])
def test_provider_requests_only_its_lookahead_window(window: int) -> None:
    wheels = [make_wheel(str(i)) for i in range(1, 5)]
    port = make_coordinator(wheels, auto_metadata=True)
    provider = Provider(port, lookahead_versions=window)
    provider.versions_cache["pkg"] = [(Version(w.version), w) for w in reversed(wheels)]
    provider._prefetch_walk_ahead("pkg", VersionRange.full())
    requests = port.calls_to("request_metadata_batch")
    if window:
        assert len(requests) == 1
        items = requests[0][0]
        assert [item[1] for item in items] == [str(i) for i in range(4, 4 - window, -1)]
    else:
        assert requests == []

    assert Provider(make_coordinator()).DEEP_PREFETCH_COUNT == 64


def test_provider_rejects_a_negative_explicit_window() -> None:
    with pytest.raises(ValueError, match="lookahead_versions"):
        Provider(make_coordinator(), lookahead_versions=-1)
