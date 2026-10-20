"""Fresh Simple API entries for parser contract tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def wheel_entry() -> dict[str, object]:
    """Return a fresh usable neighbor for malformed-entry tests."""
    return {
        "filename": "foo-1.0-py3-none-any.whl",
        "url": "https://example.com/foo/foo-1.0-py3-none-any.whl",
        "hashes": {"sha256": "a" * 64},
    }


@pytest.fixture(params=["wheel", "sdist"])
def file_entry(
    wheel_entry: dict[str, object], request: pytest.FixtureRequest
) -> dict[str, object]:
    """Exercise record construction for each supported distribution format."""
    if request.param == "sdist":
        return wheel_entry | {
            "filename": "foo-1.0.tar.gz",
            "url": "https://example.com/foo/foo-1.0.tar.gz",
        }
    return wheel_entry
