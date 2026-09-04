"""Run the examples in the public module's docstrings.

Several examples pin outputs chosen by the engine. A changed output changes the
package's documented behavior.
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
