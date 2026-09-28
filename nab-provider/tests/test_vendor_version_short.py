"""Version parsing for short numeric releases and their fallbacks."""

from itertools import product

import pytest

from nab_provider._vendor.packaging.version import InvalidVersion, Version


def test_single_digit_releases() -> None:
    for parts in product(range(10), repeat=3):
        text = ".".join(map(str, parts))
        parsed = Version(text)
        general = Version(f" {text} ")

        assert parsed.release == parts
        assert str(parsed) == text
        assert parsed == general
        assert hash(parsed) == hash(general)


def test_empty_release_components() -> None:
    for parts in product("1.", repeat=3):
        if "." not in parts:
            continue
        with pytest.raises(InvalidVersion):
            Version(".".join(parts))


@pytest.mark.parametrize("text", ["1.2", "1.23.4", "1.2.3rc1", "1.a.2", "1.2.a"])
def test_other_version_shapes(text: str) -> None:
    assert Version(text) == Version(f" {text} ")


class AlternateSplit(str):
    """Preserve a string subclass's numeric component parsing."""

    __slots__ = ()

    def split(self, sep: str | None = None, maxsplit: int = -1) -> list[str]:
        return ["9", "8", "7"]


def test_string_subclass_split() -> None:
    assert Version(AlternateSplit("1.2.3")).release == (9, 8, 7)
