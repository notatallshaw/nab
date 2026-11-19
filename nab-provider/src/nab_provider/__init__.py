"""Python packaging rules and candidates for the generic resolver.

Supported imports:

    nab_provider.diagnostics  Diagnostic, ResolutionMessage
    nab_provider.priority  compute_tier
    nab_provider.ranges  VersionRange
    nab_provider.records  IndexConfig
    nab_provider.target  ResolveTarget
    nab_provider.environment  classify_dependency, marker_environment
    nab_provider.requirements_file  expand_project_requirements
    nab_provider.preference  PreferenceSearch, PreferenceScope, PreparationStatus,
                             PreferenceCatalogue, DependencyData,
                             IncompletePreferenceError, UnavailableMetadataError
"""
