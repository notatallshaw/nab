"""IO-free resolution core shared by nab-index and nab-project.

Supported imports:

    nab_provider.diagnostics  Diagnostic, ResolutionMessage
    nab_provider.priority  compute_tier
    nab_provider.environment  classify_dependency, marker_environment
    nab_provider.preference  CataloguePreference, PreferenceSearch, PreferenceScope,
                             PreparationStatus,
                             PreferenceCatalogue, DependencyData,
                             IncompletePreferenceError, UnavailableMetadataError
    nab_provider.preference_provider  PreferenceProvider, MetadataProxy,
                                      PermissionProxy, Package
    nab_provider.version_candidates  VersionCandidate, VersionCandidateData,
                                     VersionCatalogue,
                                     PreparedVersionCatalogue, is_pin
"""
