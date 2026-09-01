"""Deferred integrity parsing on the Simple-API records."""

from __future__ import annotations

import threading

import pytest

from nab_provider import records
from nab_provider.records import (
    SdistFile,
    WheelFile,
    defer_hashes,
    defer_sidecar_hash,
    rehydrated_wheel,
)

DIGEST = "a" * 64
SHA512_DIGEST = "b" * 128
SHA256 = "sha256"
SHA512 = "sha512"
_TIMEOUT = 10.0


def _deferred_wheel(hashes: object, *, sidecar: object) -> WheelFile:
    """A wheel holding ``hashes`` and ``sidecar`` as the index served them."""
    wheel = WheelFile(
        filename="pkg-1.0-py3-none-any.whl",
        url="https://files.example/pkg-1.0-py3-none-any.whl",
        version="1.0",
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )
    defer_hashes(wheel, hashes)
    defer_sidecar_hash(wheel, sidecar)
    return wheel


def _rehydrated_wheel(table: dict[object, str]) -> WheelFile:
    """A wheel rebuilt from a cached listing row that holds ``table`` for both fields."""
    return rehydrated_wheel(
        filename="pkg-1.0-py3-none-any.whl",
        url="https://files.example/pkg-1.0-py3-none-any.whl",
        version="1.0",
        requires_python=None,
        has_metadata=True,
        upload_time=None,
        hashes=table,
        size=None,
        metadata_hash=table,
    )


def _deferred_sdist(hashes: object) -> SdistFile:
    """A source distribution holding ``hashes`` as the index served them."""
    sdist = SdistFile(
        filename="pkg-1.0.tar.gz",
        url="https://files.example/pkg-1.0.tar.gz",
        version="1.0",
        requires_python=None,
        upload_time=None,
    )
    defer_hashes(sdist, hashes)
    return sdist


def test_a_first_read_parses_the_table_the_index_served() -> None:
    wheel = _deferred_wheel({"SHA256": DIGEST.upper()}, sidecar={"sha256": DIGEST})

    assert wheel.hashes == ((SHA256, DIGEST),)
    assert wheel.metadata_hash == (SHA256, DIGEST)


def test_a_second_read_returns_the_value_the_first_stored() -> None:
    wheel = _deferred_wheel({"sha256": DIGEST}, sidecar={"sha256": DIGEST})
    hashes = wheel.hashes
    metadata_hash = wheel.metadata_hash

    assert wheel.hashes is hashes
    assert wheel.metadata_hash is metadata_hash


def test_a_lone_sha256_table_is_held_as_the_digest_alone() -> None:
    """The compact form is why a deferred record costs less than the served dict."""
    wheel = _deferred_wheel({"sha256": DIGEST}, sidecar={"sha256": DIGEST})

    assert wheel._raw_hashes == DIGEST
    assert wheel._raw_metadata == DIGEST


def test_a_lone_other_algorithm_table_is_held_as_a_pair() -> None:
    """Only sha256 can be held as the digest alone; another name is kept beside it."""
    wheel = _deferred_wheel(
        {"sha512": SHA512_DIGEST}, sidecar={"sha512": SHA512_DIGEST}
    )

    assert wheel._raw_hashes == (SHA512, SHA512_DIGEST)
    assert wheel._raw_metadata == (SHA512, SHA512_DIGEST)


def test_a_lone_sha256_whose_digest_is_not_a_string_keeps_its_name() -> None:
    """Only a string digest can stand for the table, so a non-string keeps its name."""
    wheel = _deferred_wheel({"sha256": 7}, sidecar={"sha256": 7})

    assert wheel._raw_hashes == (SHA256, 7)
    assert wheel._raw_metadata == (SHA256, 7)

    assert wheel.raw_hashes() == {"sha256": 7}
    assert wheel.raw_sidecar() == {"sha256": 7}


def test_a_many_algorithm_table_is_held_as_it_stands() -> None:
    table = {"sha256": DIGEST, "sha512": "f" * 128}

    wheel = _deferred_wheel(table, sidecar=table)

    assert wheel._raw_hashes == table
    assert wheel._raw_metadata == table


def test_a_rehydrated_lone_sha256_table_is_held_as_the_digest_alone() -> None:
    """A row rebuilt from a cached listing takes the same compact form."""
    wheel = _rehydrated_wheel({"sha256": DIGEST})

    assert wheel._raw_hashes == DIGEST
    assert wheel._raw_metadata == DIGEST

    assert wheel.hashes == ((SHA256, DIGEST),)
    assert wheel.metadata_hash == (SHA256, DIGEST)
    assert wheel.raw_hashes() == {"sha256": DIGEST}


def test_a_rehydrated_one_algorithm_table_keeps_the_name_the_row_carried() -> None:
    """One blob decodes to one name object, so re-interning it per row buys nothing."""
    algo = b"sha512".decode()
    assert algo is not SHA512

    wheel = _rehydrated_wheel({algo: SHA512_DIGEST})
    hashes, metadata = wheel._raw_hashes, wheel._raw_metadata
    assert isinstance(hashes, tuple)
    assert isinstance(metadata, tuple)

    assert hashes == (algo, SHA512_DIGEST)
    assert hashes[0] is algo
    assert metadata[0] is algo

    # Reading the field parses and interns, so the pair a caller sees is unchanged.
    assert wheel.hashes == ((SHA512, SHA512_DIGEST),)
    assert wheel.hashes[0][0] is SHA512


@pytest.mark.parametrize(
    "table", [{"sha256": DIGEST, "sha512": "f" * 128}, {7: DIGEST}]
)
def test_a_rehydrated_table_the_pair_form_cannot_hold_is_kept_whole(
    table: dict[object, str],
) -> None:
    """Several algorithms, or a name that is not a string, is held as it stands."""
    wheel = _rehydrated_wheel(table)

    assert wheel._raw_hashes == table
    assert wheel._raw_metadata == table


def test_a_read_keeps_the_table_it_parsed() -> None:
    wheel = _deferred_wheel({"sha256": DIGEST}, sidecar={"sha256": DIGEST})

    assert wheel.hashes == ((SHA256, DIGEST),)
    assert wheel.raw_hashes() == {"sha256": DIGEST}

    assert wheel.metadata_hash == (SHA256, DIGEST)
    assert wheel.raw_sidecar() == {"sha256": DIGEST}


@pytest.mark.parametrize(
    ("field", "parser", "expected"),
    [
        ("hashes", "parse_hash_table", ((SHA256, DIGEST),)),
        ("metadata_hash", "sidecar_hash", (SHA256, DIGEST)),
    ],
)
def test_two_threads_reading_one_deferred_field_both_get_it(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    parser: str,
    expected: object,
) -> None:
    """A listing's records are read by the fetching and the resolving thread."""
    wheel = _deferred_wheel({"sha256": DIGEST}, sidecar={"sha256": DIGEST})
    parse = getattr(records, parser)
    both_reading = threading.Barrier(2, timeout=_TIMEOUT)

    def wait_then_parse(value: object) -> object:
        both_reading.wait()
        return parse(value)

    monkeypatch.setattr(records, parser, wait_then_parse)

    seen: list[object] = []
    failures: list[AttributeError] = []

    def read() -> None:
        try:
            seen.append(getattr(wheel, field))
        except AttributeError as exc:
            failures.append(exc)

    readers = [threading.Thread(target=read) for _ in range(2)]
    for reader in readers:
        reader.start()
    for reader in readers:
        reader.join(_TIMEOUT)

    assert failures == []
    assert seen == [expected, expected]


def test_a_value_that_is_not_a_table_defers_nothing() -> None:
    wheel = _deferred_wheel(["sha256", DIGEST], sidecar=True)

    assert wheel.raw_hashes() is None
    assert wheel.raw_sidecar() is None
    assert wheel.hashes == ()
    assert wheel.metadata_hash is None


def test_a_table_keyed_by_a_non_string_defers_as_it_stands() -> None:
    wheel = _deferred_wheel({7: DIGEST}, sidecar=True)

    assert wheel.raw_hashes() == {7: DIGEST}
    assert wheel.hashes == ()


def test_a_source_distribution_has_no_sidecar_hash_to_defer() -> None:
    sdist = _deferred_sdist({"sha256": DIGEST})

    with pytest.raises(AttributeError, match="metadata_hash"):
        sdist.metadata_hash  # noqa: B018


def test_deferral_does_not_answer_for_an_unknown_attribute() -> None:
    wheel = _deferred_wheel({"sha256": DIGEST}, sidecar={"sha256": DIGEST})

    with pytest.raises(AttributeError, match="no_such_field"):
        wheel.no_such_field  # noqa: B018
