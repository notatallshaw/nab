"""Contract for ``ROOT``, the virtual root package sentinel.

The resolver's package-keyed dicts hash it on every lookup, so a Python
``__hash__`` on it would cost a frame each time.
"""

from __future__ import annotations

from nab_resolver.root import ROOT


class TestRootSentinel:
    def test_the_sentinel_hashes_through_object(self) -> None:
        assert type(ROOT).__hash__ is object.__hash__

    def test_the_sentinel_compares_by_identity(self) -> None:
        """A second instance of the class is a distinct key, not an alias."""
        other = type(ROOT)()

        assert other != ROOT
        assert {ROOT: "root", other: "other"}[ROOT] == "root"

    def test_the_sentinel_reprs_as_root(self) -> None:
        assert repr(ROOT) == "<root>"
