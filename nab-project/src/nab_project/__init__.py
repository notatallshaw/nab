"""Python package resolver built on the nab resolver core.

Supported imports:

    nab_project.input_source  InputFormat, ScriptMetadataError, read_input_document
    nab_project.script_metadata  ScriptMetadata, read_script_metadata
    nab_project.build_backend  project_metadata
    nab_project.diagnostics  ResolutionMessage
    nab_project.lock_validation  snapshot_inputs, selection_inputs_digest,
        content_digest, make_validation_record, validation_digest, VALIDATION_VERSION
    nab_project.lock_check  check_saved_lock, can_reuse_saved_lock, LockConsistencyError
    nab_project.lock_policy  LockPolicy
    nab_project.lock_preferences  LockPreferences, read_lock_preferences
    nab_project.lockfile  resolve_dependency_references
    nab_project.resolve  plan_project_inputs
    nab_project.toml_writer  dumps, dumps_value
"""
