"""Python package resolver built on the nab resolver core.

Supported imports:

    nab_project.config  ConfigConflictError, ConfigConflictKind,
        EnvironmentConfig, NabProjectConfig, plan_targets,
        read_pyproject_config
    nab_project.config_overrides  ProjectOverride, ProjectTableOverride,
        ProjectOverrides, ProjectOverrideError
    nab_project.config_schema  HostKey, HOST_SETTINGS, ProjectValueContext,
        Setting, SETTINGS, project_file_conflict
    nab_project.config_values  DIST_POLICY_REPLACEMENTS, MatrixConfig,
        SourceConfigError, ProjectFileConflictError, matrix_from_config, matrix_mode,
        check_package_override_overlap
    nab_project.input_source  InputFormat, ScriptMetadataError, read_input_document
    nab_project.script_metadata  ScriptMetadata, read_script_metadata
    nab_project.build_backend  project_metadata
    nab_project.diagnostics  ResolutionMessage
    nab_project.lock_validation  snapshot_inputs, selection_inputs_digest,
        content_digest, make_validation_record, validation_digest, VALIDATION_VERSION
    nab_project.lock_check  check_saved_lock, LockConsistencyError
    nab_project.lockfile  resolve_dependency_references
    nab_project.resolve  plan_project_inputs
    nab_project.toml_writer  dumps
"""
