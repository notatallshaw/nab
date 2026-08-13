"""nab-project's build of :class:`nab_provider.testing.FakeFetchPort`.

Wraps :func:`nab_provider.testing.make_coordinator` with the handlers a host
owns, wired to what nab-project runs in production: declared-source
materialisation, the remote-sdist build, and reading a wheel off disk.

``sdist_pyproject_toml`` replaces the provider's ``sdist_pyproject``: it takes
TOML text and hands the fake the parsed table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nab_index.local_index import read_wheel_metadata
from nab_project._build_remote import build_remote_sdist
from nab_project._sources import materialize_source
from nab_project._toml import parse_pyproject_table
from nab_provider.errors import UnsupportedWheelError
from nab_provider.testing import FakeFetchPort
from nab_provider.testing import make_coordinator as _make_coordinator

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["FakeFetchPort", "make_coordinator"]


def _read_wheel(path: Path) -> str | None:
    """Read a wheel's own METADATA, as the local index client does."""
    try:
        return read_wheel_metadata(path)
    except UnsupportedWheelError:
        return None


def make_coordinator(
    *args: Any,
    sdist_pyproject_toml: str | None = None,
    **kwargs: Any,
) -> FakeFetchPort:
    """Build the provider's fake with nab-project's host handlers wired in."""
    return _make_coordinator(
        *args,
        sdist_pyproject=(
            None
            if sdist_pyproject_toml is None
            else parse_pyproject_table(sdist_pyproject_toml)
        ),
        materialize=materialize_source,
        build_sdist=build_remote_sdist,
        read_wheel=_read_wheel,
        **kwargs,
    )
