"""nab-project suite fixtures."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from nab_project import toml_io

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager


@contextmanager
def _record_parses() -> Iterator[list[str]]:
    parsed: list[str] = []
    real_loads = toml_io.loads

    def recording_loads(text: str) -> dict[str, Any]:
        parsed.append(text)
        return real_loads(text)

    with patch.object(toml_io, "loads", recording_loads):
        yield parsed


@pytest.fixture
def record_parses() -> Callable[[], AbstractContextManager[list[str]]]:
    """Record the text of every TOML document parsed inside the block.

    The hook sits on ``toml_io.loads``, the funnel every nab parse reaches,
    so a caller that opens a file and parses it some other way is recorded
    too. Counting one file's own text in the result keeps the other files a
    command reads out of the tally.
    """
    return _record_parses
