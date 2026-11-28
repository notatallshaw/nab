"""A query must encode dependencies and actual decisions in the same integer domain."""

from nab_provider.candidate_tokens import CandidateTokenRange
from nab_resolver.ranges import Range


def test_candidate_runs_keep_omitted_and_future_tokens_out() -> None:
    tokens = CandidateTokenRange.from_versions(iter((4, 2, 1, 4, 6, 5)))

    assert [value for value in range(8) if value in tokens] == [1, 2, 4, 5, 6]
    assert tokens - CandidateTokenRange.singleton(
        5
    ) == CandidateTokenRange.from_versions((1, 2, 4, 6))
    assert CandidateTokenRange.from_versions(()) == Range.empty()


def test_adjacent_decisions_cover_the_same_domain_as_the_encoded_edges() -> None:
    edges = CandidateTokenRange.from_versions((2, 3))
    decisions = CandidateTokenRange.singleton(2) | CandidateTokenRange.singleton(3)

    assert edges == decisions == Range.between(2, 4)
    assert (edges - decisions).is_empty
