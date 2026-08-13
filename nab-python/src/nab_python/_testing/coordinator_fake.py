"""nab-python's build of :class:`nab_provider.testing.FakeFetchPort`.

The provider's fake serves everything a host does not have to own.  The three
things it leaves to a host are exactly the three nab-python supplies in
production, so this wraps :func:`nab_provider.testing.make_coordinator` with
them: declared-source materialisation, the remote-sdist build, and reading a
wheel that a listing serves off disk.

The one signature difference is ``sdist_pyproject_toml``, which takes the TOML
a bundled ``pyproject.toml`` holds.  Parsing it is nab-python's, so the text is
parsed here and the provider's fake is handed the table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nab_index.local_index import read_wheel_metadata
from nab_provider.errors import UnsupportedWheelError
from nab_provider.testing import FakeFetchPort
from nab_provider.testing import make_coordinator as _make_coordinator
from nab_python._build_remote import build_remote_sdist
from nab_python._sources import materialize_source
from nab_python._toml import parse_pyproject_table

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
    """Build the provider's fake with nab-python's three host handlers wired in."""
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
