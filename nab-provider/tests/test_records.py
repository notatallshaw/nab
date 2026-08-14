"""Tests for the value records a resolve is expressed in."""

from __future__ import annotations

import copy
import pickle
from collections.abc import Callable

import pytest

from nab_provider.records import WheelFile

WHEEL_URL = "https://pypi.example/pkg-1.0-py3-none-any.whl"


def make_wheel(*, has_metadata: bool = True) -> WheelFile:
    """A wheel whose URL carries a PEP 503 hash fragment."""
    return WheelFile(
        filename="pkg-1.0-py3-none-any.whl",
        url=f"{WHEEL_URL}#sha256=ab",
        version="1.0",
        requires_python=None,
        has_metadata=has_metadata,
        upload_time=None,
    )


def round_trip_pickle(wheel: WheelFile) -> WheelFile:
    """A pickled and restored copy of ``wheel``."""
    return pickle.loads(pickle.dumps(wheel))  # noqa: S301


def test_metadata_url_appends_suffix_to_path() -> None:
    """The suffix goes on the path, so the hash fragment is dropped."""
    assert make_wheel().metadata_url == f"{WHEEL_URL}.metadata"


def test_metadata_url_none_without_sidecar() -> None:
    """A wheel the index advertised no sidecar for has no metadata URL."""
    assert make_wheel(has_metadata=False).metadata_url is None


def test_metadata_url_reuses_first_result() -> None:
    """A second read returns the first string, not an equal rebuild."""
    wheel = make_wheel()

    assert wheel.metadata_url is wheel.metadata_url


def test_memo_leaves_equality_and_hashing_alone() -> None:
    """The memo writes past the frozen guard; the fields still decide both."""
    read, unread = make_wheel(), make_wheel()
    assert read.metadata_url is not None

    assert read == unread
    assert hash(read) == hash(unread)
    assert len({read, unread}) == 1


def test_memo_stays_out_of_repr() -> None:
    """The memo is not a field, so it never reaches the generated repr."""
    wheel = make_wheel()
    assert wheel.metadata_url is not None

    assert "_metadata_url" not in repr(wheel)


@pytest.mark.parametrize("clone", [copy.copy, round_trip_pickle])
def test_wheel_round_trips_either_side_of_first_read(
    clone: Callable[[WheelFile], WheelFile],
) -> None:
    """Copy and pickle go by the fields, so the memo is neither needed nor carried."""
    unread = make_wheel()
    assert clone(unread) == unread

    read = make_wheel()
    assert read.metadata_url == clone(read).metadata_url
