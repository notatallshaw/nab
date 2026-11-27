"""Preference search interfaces for hosts supplying immutable candidate facts."""

from nab_candidates.preference import (
    DependencyData,
    IncompletePreferenceError,
    PreferenceCatalogue,
    PreferenceScope,
    PreferenceSearch,
    PreparationStatus,
    UnavailableMetadataError,
)

__all__ = [
    "DependencyData",
    "IncompletePreferenceError",
    "PreferenceCatalogue",
    "PreferenceScope",
    "PreferenceSearch",
    "PreparationStatus",
    "UnavailableMetadataError",
]
