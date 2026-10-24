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


@pytest.fixture(params=["wheel", "sdist", "zip-sdist"])
def file_entry(
    wheel_entry: dict[str, object], request: pytest.FixtureRequest
) -> dict[str, object]:
    """Exercise record construction for each supported distribution format."""
    if request.param in {"sdist", "zip-sdist"}:
        suffix = "tar.gz" if request.param == "sdist" else "zip"
        return wheel_entry | {
            "filename": f"foo-1.0.{suffix}",
            "url": f"https://example.com/foo/foo-1.0.{suffix}",
        }
    return wheel_entry
