"""Select compatible Python package files and explain why a yanked file was chosen.

Supported imports:

    nab_candidates.resolution  PreparedResolution, PreparedSolution
    nab_candidates.provider  PreparedCatalogue, PreferenceProvider, CataloguePreference,
                             MetadataProxy, PermissionProxy, Package
    nab_candidates.versions  PreparedVersionCatalogue, VersionCandidate,
                             VersionCandidateData, VersionCatalogue, is_pin
    nab_candidates.preference  PreferenceSearch, PreferenceScope, PreferenceCatalogue,
                               DependencyData, PreparationStatus,
                               IncompletePreferenceError, UnavailableMetadataError
    nab_candidates.lookahead  DecisionConflict, RangeConflict, find_dependency_conflict,
                              queue_blocker, LOOKAHEAD_ABORT_THRESHOLD,
                              MAX_FORCE_BACKTRACKS_PER_PACKAGE
    nab_candidates.records  Record, HashableRecord, record_fields
"""
