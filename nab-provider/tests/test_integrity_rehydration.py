"""Integrity handoffs preserve lazy records and custom raw readers."""

from __future__ import annotations

from typing import ClassVar

import pytest

from nab_provider import records
from nab_provider._compat import override
from nab_provider.records import (
    SdistFile,
    WheelFile,
    parse_hash_table,
    rehydrated_sdist,
    rehydrated_wheel,
    release_wheel_payload,
    sidecar_hash,
)
from nab_provider.testing import make_wheel

DIGEST = "ab" * 32
OTHER_DIGEST = "cd" * 64
PARSED_HASHES = (("sha256", DIGEST),)
PARSED_SIDECAR = ("sha256", DIGEST)


class DigestText(str):
    """Retain the string-subclass form accepted by rehydration factories."""

    __slots__ = ()


class CustomWheel(WheelFile):
    """Supply integrity through the existing public raw-reader overrides."""

    hashes_override: ClassVar[object] = None
    sidecar_override: ClassVar[object] = None

    @override
    def raw_hashes(self) -> object:
        return self.hashes_override

    @override
    def raw_sidecar(self) -> object:
        return self.sidecar_override


class CustomSdist(SdistFile):
    """Supply a source distribution's hashes through its raw reader."""

    hashes_override: ClassVar[object] = None

    @override
    def raw_hashes(self) -> object:
        return self.hashes_override


def deferred_wheel(cell: dict[object, object] | str = DIGEST) -> WheelFile:
    """Build a native wheel with both integrity fields deferred."""
    return rehydrated_wheel(
        filename="pkg-1.0-py3-none-any.whl",
        url="https://example.com/pkg.whl",
        version="1.0",
        requires_python=None,
        has_metadata=True,
        upload_time=None,
        hashes=cell,
        size=None,
        metadata_hash=cell,
    )


@pytest.mark.parametrize(
    ("raw", "cell"),
    [
        ({}, {}),
        ({"sha256": DIGEST}, DIGEST),
        ({"sha256": "not-a-digest"}, "not-a-digest"),
        ({"sha256": False}, {"sha256": False}),
        ({"SHA256": DIGEST}, {"SHA256": DIGEST}),
        ({"sha512": OTHER_DIGEST}, {"sha512": OTHER_DIGEST}),
        ({1: DIGEST}, {1: DIGEST}),
        (
            {"sha256": DIGEST, "sha512": OTHER_DIGEST},
            {"sha256": DIGEST, "sha512": OTHER_DIGEST},
        ),
        (DigestText(DIGEST), {"sha256": DigestText(DIGEST)}),
        ({"sha256": DigestText(DIGEST)}, {"sha256": DigestText(DIGEST)}),
    ],
)
def test_native_handoff_preserves_integrity(
    raw: dict[object, object] | str, cell: object
) -> None:
    wheel = deferred_wheel(raw)
    table = {"sha256": raw} if isinstance(raw, str) else raw

    assert wheel.hashes_for_rehydration() == cell
    assert wheel.sidecar_for_rehydration() == cell
    assert wheel.raw_hashes() == table
    assert wheel.raw_sidecar() == table

    assert wheel.hashes == parse_hash_table(table)
    assert wheel.metadata_hash == sidecar_hash(table)


def test_native_compact_handoff_skips_table_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = deferred_wheel()
    sdist = rehydrated_sdist(
        "pkg-1.0.tar.gz",
        "https://example.com/pkg.tar.gz",
        "1.0",
        None,
        None,
        DIGEST,
        None,
    )
    expanded: list[object] = []
    original = records._expand_table

    def observe(held: object) -> object:
        expanded.append(held)
        return original(held)

    monkeypatch.setattr(records, "_expand_table", observe)

    assert wheel.hashes_for_rehydration() == DIGEST
    assert wheel.sidecar_for_rehydration() == DIGEST
    assert sdist.hashes_for_rehydration() == DIGEST
    assert expanded == []

    assert wheel.raw_hashes() == {"sha256": DIGEST}
    assert wheel.raw_sidecar() == {"sha256": DIGEST}
    assert sdist.raw_hashes() == {"sha256": DIGEST}
    assert expanded == [DIGEST, DIGEST, DIGEST]


def test_parsed_handoff_keeps_parsed_fields() -> None:
    wheel = make_wheel(hashes=PARSED_HASHES, metadata_hash=PARSED_SIDECAR)
    sdist = SdistFile(
        "pkg-1.0.tar.gz",
        "https://example.com/pkg.tar.gz",
        "1.0",
        None,
        None,
        hashes=PARSED_HASHES,
    )

    assert wheel.hashes_for_rehydration() is PARSED_HASHES
    assert wheel.sidecar_for_rehydration() is PARSED_SIDECAR
    assert sdist.hashes_for_rehydration() is PARSED_HASHES


@pytest.mark.parametrize("parsed", [False, True])
def test_released_handoff_keeps_only_materialized_integrity(parsed: bool) -> None:
    wheel = deferred_wheel()
    expected_hashes = wheel.hashes if parsed else ()
    expected_sidecar = wheel.metadata_hash if parsed else None
    release_wheel_payload(wheel)

    assert wheel.hashes_for_rehydration() == expected_hashes
    assert wheel.sidecar_for_rehydration() == expected_sidecar


def test_handoff_compacts_a_raw_table_mutated_after_construction() -> None:
    table: dict[object, object] = {"sha256": DIGEST, "sha512": OTHER_DIGEST}
    wheel = deferred_wheel(table)
    del table["sha512"]

    assert wheel.hashes_for_rehydration() == DIGEST
    assert wheel.sidecar_for_rehydration() == DIGEST


@pytest.mark.parametrize(
    ("raw_hashes", "raw_sidecar", "hashes_cell", "sidecar_cell"),
    [
        (None, None, PARSED_HASHES, PARSED_SIDECAR),
        ({"sha256": DIGEST}, {"sha256": DIGEST}, DIGEST, DIGEST),
        (PARSED_HASHES, PARSED_SIDECAR, PARSED_HASHES, PARSED_SIDECAR),
        (0, False, 0, False),
    ],
)
def test_custom_handoff_respects_raw_reader_overrides(
    monkeypatch: pytest.MonkeyPatch,
    raw_hashes: object,
    raw_sidecar: object,
    hashes_cell: object,
    sidecar_cell: object,
) -> None:
    monkeypatch.setattr(CustomWheel, "hashes_override", raw_hashes)
    monkeypatch.setattr(CustomWheel, "sidecar_override", raw_sidecar)
    monkeypatch.setattr(CustomSdist, "hashes_override", raw_hashes)
    wheel = CustomWheel(
        "pkg-1.0-py3-none-any.whl",
        url="https://example.com/pkg.whl",
        version="1.0",
        requires_python=None,
        has_metadata=True,
        upload_time=None,
        hashes=PARSED_HASHES,
        metadata_hash=PARSED_SIDECAR,
    )
    sdist = CustomSdist(
        "pkg-1.0.tar.gz",
        "https://example.com/pkg.tar.gz",
        "1.0",
        None,
        None,
        hashes=PARSED_HASHES,
    )

    assert wheel.hashes_for_rehydration() == hashes_cell
    assert wheel.sidecar_for_rehydration() == sidecar_cell
    assert sdist.hashes_for_rehydration() == hashes_cell
