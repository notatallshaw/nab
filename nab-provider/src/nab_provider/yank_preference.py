"""Attach the shared preference search to this provider's candidate catalogue."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._compat import override
from ._vendor.packaging.ranges import VersionRange
from ._vendor.packaging.requirements import Requirement
from .preference import (
    CataloguePreference,
    IncompletePreferenceError,
    PreparationStatus,
)
from .preference import (
    PreferenceScope as CoreScope,
)
from .yank_candidates import Candidate

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ._vendor.packaging.version import Version
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
    """Retain native adoption around the shared preference search."""

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
        super().__init__(
            catalogue,
            roots,
            input_pins,
            constraints,
            max_steps=max_steps,
            refresh_incomplete=False,
        )
        self._native_catalogue = catalogue

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
        pins, provider = self.catalogue.adopt(selected)
        provider.yank_admission_sources = sources
        return pins, provider
