"""Python package resolver built on the nab resolver core.

Supported imports:

    nab_project.pyproject_files  MissingProjectTableError
    nab_project.input_source  InputFormat, InputChangedError, ScriptMetadataError,
        read_input_document
    nab_project.prepared_input  PreparedProjectInput
    nab_project.script_metadata  ScriptMetadata, read_script_metadata
    nab_project.build_backend  project_metadata
    nab_project.diagnostics  ResolutionMessage
    nab_project.lock_validation  snapshot_inputs, selection_inputs_digest,
        content_digest, make_validation_record, validation_digest, VALIDATION_VERSION
    nab_project.lock_check  check_saved_lock, can_reuse_saved_lock, LockConsistencyError
    nab_project.lock_policy  LockPolicy
    nab_project.lock_preferences  LockPreferences, read_lock_preferences
    nab_project.lockfile  resolve_dependency_references
    nab_project.download  run_download
    nab_project.inputs  ResolveInputs
    nab_project.resolve  ResolveResult, TargetResult, resolve_for_targets,
        plan_project_inputs, complete_project_requirements
    nab_project.dependency_inputs  dependency_input_requirements
    nab_project.dependency_sources  DependencySourceReader
    nab_project.toml_writer  dumps, dumps_value
"""
