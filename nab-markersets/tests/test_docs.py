"""Run the examples in the public module's docstrings.

Three of them pin outputs the engine chooses rather than the API: a witness
version, a simplified marker string, and a restricted one. Changing any of those
is a change to what the package promises, and this is where it shows.
"""

from __future__ import annotations

import doctest

from nab_markersets import markersets


def test_the_public_docstrings_print_what_they_document() -> None:
    results = doctest.testmod(markersets, verbose=False)

    assert results.failed == 0
    assert results.attempted > 0
