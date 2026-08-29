"""Run the examples in the public module's docstrings.

Several pin outputs the engine chooses rather than the API: a witness version,
a simplified marker string, a restricted one, and the two verdicts the class
docstring uses to show where the decisions are not exact. Changing any of those
is a change to what the package promises, and this is where it shows.
"""

from __future__ import annotations

import doctest

from nab_markersets import markersets

# Pinned so deleting an example fails here rather than passing on fewer.
EXAMPLES = 17


def test_the_public_docstrings_print_what_they_document() -> None:
    results = doctest.testmod(markersets, verbose=False)

    assert results.failed == 0
    assert results.attempted == EXAMPLES
