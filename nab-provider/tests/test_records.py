"""Tests for the value records a resolve is expressed in."""

from __future__ import annotations

import copy
import inspect
import pickle
from collections.abc import Callable

import pytest

from nab_provider.records import (
    SdistFile,
    WheelFile,
    parse_hash_table,
    rehydrated_sdist,
    rehydrated_wheel,
)
from nab_provider.value import field_names

WHEEL_URL = "https://pypi.example/pkg-1.0-py3-none-any.whl"

HAND_WRITTEN_INIT_RECORDS = [WheelFile, SdistFile]


class _DigestText(str):
    """A string subtype accepted by cached-record factories."""

    __slots__ = ()


def make_wheel(
    *, has_metadata: bool = True, url: str = f"{WHEEL_URL}#sha256=ab"
) -> WheelFile:
    """A wheel whose URL carries a PEP 503 hash fragment by default."""
    return WheelFile(
        filename="pkg-1.0-py3-none-any.whl",
        url=url,
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


def test_metadata_url_none_without_a_url() -> None:
    """A released record has no URL for the suffix to go on."""
    assert make_wheel(url="").metadata_url is None


def test_metadata_url_reuses_first_result() -> None:
    """A second read returns the first string, not an equal rebuild."""
    wheel = make_wheel()

    assert wheel.metadata_url is wheel.metadata_url


def test_memo_leaves_equality_and_hashing_alone() -> None:
    """Only the declared fields determine equality and hashing."""
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


@pytest.mark.parametrize("record", HAND_WRITTEN_INIT_RECORDS)
def test_init_takes_the_fields_in_order(record: type[WheelFile | SdistFile]) -> None:
    """``__match_args__`` comes off the field list, not off this signature.

    So a parameter reordered here alone would change what a positional call
    means without changing what a pattern binds.
    """
    parameters = list(inspect.signature(record.__init__).parameters)

    assert parameters[0] == "self"
    assert parameters[1:] == list(field_names(record))


@pytest.mark.parametrize("record", HAND_WRITTEN_INIT_RECORDS)
def test_minimal_constructor_uses_expected_defaults(
    record: type[WheelFile | SdistFile],
) -> None:
    """Omitted optional fields retain the record defaults."""
    required = {
        "filename": "pkg-1.0-py3-none-any.whl",
        "url": WHEEL_URL,
        "version": "1.0",
        "requires_python": None,
        "upload_time": None,
    }
    expected = {"hashes": (), "size": None, "local_path": None}
    if record is WheelFile:
        required["has_metadata"] = False
        expected["metadata_hash"] = None
    value = record(**required)

    assert {name: getattr(value, name) for name in expected} == expected


def test_withdrawn_wheel_releases_the_original_record_slots() -> None:
    from nab_provider.records import release_wheel_payload
    from nab_provider.yanking import mark_yanked

    wheel = WheelFile(
        filename="pkg-1-py3-none-any.whl",
        url="https://index.test/pkg.whl",
        version="1",
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )
    withdrawn = mark_yanked(wheel, reason="withdrawn")
    assert isinstance(withdrawn, WheelFile)
    release_wheel_payload(withdrawn)
    assert withdrawn.url == ""
    assert not withdrawn.has_metadata
    assert withdrawn.metadata_url is None
    assert withdrawn.yanked == "withdrawn"


@pytest.mark.parametrize(
    "digest",
    [
        "ABCD",
        pytest.param(_DigestText("ABCD"), id="str-subclass"),
        "not hex",
        "",
        "\ud800",
    ],
)
def test_rehydrated_compact_hashes_keep_raw_and_parsed_values(digest: str) -> None:
    wheel = rehydrated_wheel(
        filename="pkg-1.0-py3-none-any.whl",
        url=WHEEL_URL,
        version="1.0",
        requires_python=None,
        has_metadata=True,
        upload_time=None,
        hashes=digest,
        size=None,
        metadata_hash=digest,
    )
    sdist = rehydrated_sdist(
        filename="pkg-1.0.tar.gz",
        url="https://pypi.example/pkg-1.0.tar.gz",
        version="1.0",
        requires_python=None,
        upload_time=None,
        hashes=digest,
        size=None,
    )
    assert wheel.raw_hashes() == sdist.raw_hashes() == {"sha256": digest}
    assert wheel.raw_sidecar() == {"sha256": digest}

    expected = (("sha256", "abcd"),) if digest == "ABCD" else ()
    assert wheel.hashes == sdist.hashes == expected
    assert wheel.metadata_hash == (expected[0] if expected else None)
    assert wheel.raw_hashes() == sdist.raw_hashes() == {"sha256": digest}
    assert wheel.raw_sidecar() == {"sha256": digest}


def test_compact_rehydration_defers_hash_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed: list[object] = []

    def observe(value: object) -> tuple[tuple[str, str], ...]:
        parsed.append(value)
        return parse_hash_table(value)

    monkeypatch.setattr("nab_provider.records.parse_hash_table", observe)
    sdist = rehydrated_sdist(
        filename="pkg-1.0.tar.gz",
        url="https://pypi.example/pkg-1.0.tar.gz",
        version="1.0",
        requires_python=None,
        upload_time=None,
        hashes="ABCD",
        size=None,
    )
    assert not parsed
    assert sdist.hashes == (("sha256", "abcd"),)
    assert parsed == [{"sha256": "ABCD"}]
    assert sdist.hashes == (("sha256", "abcd"),)
    assert len(parsed) == 1
