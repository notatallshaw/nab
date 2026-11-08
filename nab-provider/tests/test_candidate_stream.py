"""Keep lazy preparation, group rejection and incumbent ordering independent."""

from collections.abc import Callable

import pytest

from nab_provider.candidate_stream import (
    CandidateGroupRejectedError,
    ordered_candidates,
)


@pytest.mark.parametrize(
    ("prefer", "version", "expected"),
    [
        (True, 2, ["installed", "new"]),
        (False, 2, ["new", "installed", "old"]),
        (False, 4, ["installed", "new", "old"]),
        (False, 0, ["new", "old", "installed"]),
    ],
)
def test_incumbent_order_and_duplicate_groups(
    prefer: bool, version: int, expected: list[str]
) -> None:
    calls: list[str] = []

    def prepare(value: str) -> Callable[[], str]:
        def build() -> str:
            calls.append(value)
            return value

        return build

    groups = [(3, prepare("new")), (2, prepare("old")), (2, prepare("duplicate"))]
    assert (
        list(
            ordered_candidates(
                groups,
                incumbent=(version, "installed"),
                prefer_incumbent=prefer,
                precedes=lambda left, right: left >= right,
            )
        )
        == expected
    )
    assert "duplicate" not in calls
    assert ("old" in calls) is (not prefer or version != 2)


def test_failed_preparation_tries_an_alternate_but_invalid_group_does_not() -> None:
    calls = []

    def invalid() -> str:
        calls.append("invalid")
        raise CandidateGroupRejectedError

    def forbidden() -> str:
        pytest.fail("rejected groups must not prepare their alternate")

    groups = [
        (3, lambda: None),
        (3, lambda: "alternate"),
        (2, invalid),
        (2, forbidden),
        (1, lambda: "last"),
    ]
    assert list(
        ordered_candidates(
            groups,
            incumbent=None,
            prefer_incumbent=False,
            precedes=lambda left, right: left >= right,
        )
    ) == ["alternate", "last"]
    assert calls == ["invalid"]


def test_rejected_identity_does_not_close_its_group_or_hide_an_alternate() -> None:
    groups = [(2, lambda: "rejected"), (2, lambda: "alternate"), (1, lambda: "last")]
    candidates = ordered_candidates(
        groups,
        incumbent=(2, "rejected"),
        prefer_incumbent=True,
        precedes=lambda left, right: left >= right,
        excluded={"rejected"},
    )
    assert list(candidates) == ["alternate", "last"]
