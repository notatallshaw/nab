"""Attach the shared preference search to this provider's candidate catalogue."""

from __future__ import annotations

from typing import TYPE_CHECKING

from packaging.ranges import VersionRange
from packaging.requirements import Requirement

from ._compat import override
from .catalogue.provider import CataloguePreference
from .preference import (
    IncompletePreferenceError,
    PreparationStatus,
)
from .preference import (
    PreferenceScope as CoreScope,
)
from .yank_candidates import Candidate

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from packaging.version import Version

    from .provider import Provider
    from .yank_candidates import YankCandidates

__all__ = [
    "IncompletePreferenceError",
    "PreferenceScope",
    "PreparationStatus",
    "YankPreference",
]

PreferenceScope = CoreScope[Candidate]


class YankPreference(CataloguePreference[Candidate, VersionRange, Requirement]):
    """Retain native adoption and diagnostic statistics around the shared search."""

    def __init__(
        self,
        catalogue: YankCandidates,
        roots: Mapping[str, VersionRange],
        input_pins: Sequence[Requirement],
        constraints: Mapping[str, VersionRange],
        *,
        max_steps: int = 100_000,
    ) -> None:
        """Retain native adoption and the provider-specific incomplete-query policy."""
        self._native_catalogue = catalogue
        super().__init__(
            catalogue,
            roots,
            input_pins,
            constraints,
            max_steps=max_steps,
            refresh_incomplete=False,
        )

    @property
    @override
    def catalogue(self) -> YankCandidates:
        """Return the native catalogue used for preparation and adoption."""
        return self._native_catalogue

    def adopt(
        self, selected: Mapping[str, Candidate]
    ) -> tuple[dict[str, Version], Provider]:
        """Attach the allowing input or dependency pins to the selected provider."""
        sources = self.explain(selected)
        return self.catalogue.adopt(selected, sources)
