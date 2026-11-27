"""Proxy subclasses choose which fields identify a candidate."""

from typing import Final

import pytest

from nab_candidates.provider import MetadataProxy, PermissionProxy
from nab_candidates.records import HashableRecord


class FileMetadata(MetadataProxy):
    __slots__ = ("origin",)
    __record_compare_fields__ = ("origin",)
    origin: Final[str]

    def __init__(self, token: int, origin: str) -> None:
        super().__init__(token)
        self.origin = origin


class FilePermission(PermissionProxy):
    __slots__ = ("origin",)
    __record_compare_fields__ = ("origin",)

    def __init__(self, token: int, origin: str) -> None:
        super().__init__(token)
        self.origin = origin


@pytest.mark.parametrize("kind", [FileMetadata, FilePermission])
def test_proxy_subclasses_can_key_by_file_origin(
    kind: type[FileMetadata] | type[FilePermission],
) -> None:
    first = kind(1, "first")
    same_file = kind(2, "first")
    other = kind(1, "other")

    assert first == same_file
    assert first != other
    assert {first: "seen"}[same_file] == "seen"


def test_record_without_explicit_fields_uses_its_positional_fields() -> None:
    class File(HashableRecord):
        __slots__ = __match_args__ = ("name",)

        def __init__(self, name: str) -> None:
            self.name = name

    assert File("alpha") == File("alpha")
    assert File("alpha") != File("beta")
