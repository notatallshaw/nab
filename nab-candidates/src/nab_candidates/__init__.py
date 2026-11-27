"""Resolve stable file identities and prepared Python dependency metadata.

Supported imports:

    nab_candidates.resolution  PreparedResolution, PreparedSolution
    nab_candidates.provider  PreparedCatalogue, PreferenceProvider, CataloguePreference,
                             MetadataProxy, PermissionProxy, Package
    nab_candidates.versions  PreparedVersionCatalogue, VersionCandidate,
                             VersionCandidateData, VersionCatalogue, is_pin
    nab_candidates.preference  PreferenceSearch, PreferenceScope, PreferenceCatalogue,
                               DependencyData, PreparationStatus,
                               IncompletePreferenceError, UnavailableMetadataError
    nab_candidates.records  Record, HashableRecord, record_fields
"""
