"""Split proxy extras without changing plain or malformed package keys."""

from __future__ import annotations

import pytest

from nab_provider.extra_keys import split_extra


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ("", None)),
        ("Some.Name", ("Some.Name", None)),
        ("dep[Extra_Name]", ("dep", "extra-name")),
        ("dep[extra.name]", ("dep", "extra-name")),
        ("dep[]", ("dep[]", None)),
        ("dep[", ("dep[", None)),
        ("dep[extra", ("dep[extra", None)),
        ("dep[extra]]", ("dep[extra]]", None)),
    ],
)
def test_split_extra_retains_key_and_normalizes_only_the_extra(
    text: str, expected: tuple[str, str | None]
) -> None:
    assert split_extra(text) == expected
