"""Optional Simple API fields on wheel and sdist records."""

from __future__ import annotations

import pytest

from nab_index.client import _parse_files
from nab_provider.records import WheelFile


@pytest.mark.parametrize(
    ("hashes", "expected"),
    [
        pytest.param({"sha256": "A" * 64}, {"sha256": "a" * 64}, id="digest"),
        pytest.param({"SHA256": "a" * 64}, {"sha256": "a" * 64}, id="algorithm"),
        pytest.param(
            {"sha256": "A" * 64, "sha512": "B" * 128},
            {"sha256": "a" * 64, "sha512": "b" * 128},
            id="multiple-digests",
        ),
        pytest.param(
            {"SHA256": "a" * 64, "Sha512": "b" * 128},
            {"sha256": "a" * 64, "sha512": "b" * 128},
            id="multiple-algorithms",
        ),
    ],
)
def test_hashes_are_normalized(
    file_entry: dict[str, object], hashes: dict[str, str], expected: dict[str, str]
) -> None:
    files = _parse_files(
        {"files": [file_entry | {"hashes": hashes}]}, "https://example.com/", "foo"
    )

    assert len(files) == 1
    assert dict(files[0].hashes) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(4096, 4096, id="positive"),
        pytest.param(0, 0, id="zero"),
        pytest.param(True, None, id="true-is-not-a-size"),
        pytest.param(False, None, id="false-is-not-a-size"),
        pytest.param(1.5, None, id="float"),
        pytest.param("9", None, id="string"),
        pytest.param(-1, None, id="negative"),
        pytest.param(None, None, id="null"),
    ],
)
def test_size(
    file_entry: dict[str, object], value: object, expected: int | None
) -> None:
    files = _parse_files(
        {"files": [file_entry | {"size": value}]}, "https://example.com/", "foo"
    )

    assert len(files) == 1
    assert files[0].size == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("2024-06-21T00:00:00Z", "2024-06-21T00:00:00Z", id="timestamp"),
        pytest.param(1719000000, None, id="integer"),
        pytest.param(True, None, id="boolean"),
        pytest.param(None, None, id="null"),
    ],
)
def test_upload_time(
    file_entry: dict[str, object], value: object, expected: str | None
) -> None:
    files = _parse_files(
        {"files": [file_entry | {"upload-time": value}]}, "https://example.com/", "foo"
    )

    assert len(files) == 1
    assert files[0].upload_time == expected


def test_absent_optional_fields(file_entry: dict[str, object]) -> None:
    files = _parse_files({"files": [file_entry]}, "https://example.com/", "foo")

    assert len(files) == 1
    assert files[0].size is None
    assert files[0].upload_time is None


@pytest.mark.parametrize("value", ["a" * 64, [["sha256", "a" * 64]], True, 42, None])
def test_non_table_hashes_are_ignored(
    file_entry: dict[str, object], value: object
) -> None:
    files = _parse_files(
        {"files": [file_entry | {"hashes": value}]}, "https://example.com/", "foo"
    )

    assert len(files) == 1
    assert files[0].hashes == ()


@pytest.mark.parametrize(
    ("value", "advertised", "expected"),
    [
        (True, True, None),
        (False, False, None),
        ({"sha256": "A" * 64}, True, ("sha256", "a" * 64)),
        ("a" * 64, False, None),
    ],
)
def test_core_metadata_preserves_table_and_boolean_meanings(
    wheel_entry: dict[str, object],
    value: object,
    expected: tuple[str, str] | None,
    *,
    advertised: bool,
) -> None:
    entry = wheel_entry | {
        "core-metadata": value,
        "dist-info-metadata": {"sha256": "b" * 64},
    }
    files = _parse_files({"files": [entry]}, "https://example.com/", "foo")

    assert len(files) == 1
    wheel = files[0]
    assert isinstance(wheel, WheelFile)
    assert wheel.has_metadata is advertised
    assert wheel.metadata_hash == expected
