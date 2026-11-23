"""Shared target, wheel metadata and diagnostic helpers for provider tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nab_provider.target import ResolveTarget
from nab_provider.testing import make_metadata

if TYPE_CHECKING:
    from nab_provider.provider import Provider

PYTHON_312 = ResolveTarget.for_host_python("3.12.0")
SECURITY_EXTRA_METADATA = make_metadata(
    "foo",
    "1.0",
    "bar>=1.0",
    'cryptography>=2.0; extra == "security"',
    provides_extra=("security",),
)


def short_reason(provider: Provider, package: str) -> str | None:
    """Return the one line ``package``'s diagnostic prints at default verbosity."""
    diagnostic = provider.get_no_versions_reason(package)
    return None if diagnostic is None else diagnostic.short
