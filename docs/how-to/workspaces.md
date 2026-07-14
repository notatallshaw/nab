# Lock a workspace

> [!WARNING]
> Workspace support is experimental. Discovery rules, member
> validation, and the auto-promoted build policy may change.

A workspace is a parent project that declares one or more in-tree
members. When `nab lock` runs against a member (or the root itself),
the resolver prefers the in-tree members over PyPI for any package
whose canonical name matches a member's `[project].name`.

## Declaring a workspace

The workspace root carries a `[tool.nab.workspace]` table:

```toml
# /repo/pyproject.toml
[tool.nab.workspace]
members = [
    "packages/core",
    "packages/extras",
    "tools/cli",
]
```

`workspace` is a project-scope key like any other, so the same table
can be written at the top level of the project-directory `nab.toml`
instead:

```toml
# /repo/nab.toml
[workspace]
members = ["packages/core", "packages/extras", "tools/cli"]
```

`members` is a list of literal directory paths relative to the
workspace root. Globs (`*`, `?`, `[`) are rejected; spell out each
member.

Every listed member must contain a `pyproject.toml` with a
`[project].name`. Two members cannot share the same canonical name
(`Foo_Bar` and `foo-bar` collide). Both raise
`WorkspaceDiscoveryError` at config-parse time.

## How discovery works

The project being locked declares the workspace, in either of its two
config files, and its `members` are read relative to that project's
directory. That is the path `nab lock` takes at a workspace root.

When `nab lock <member>/pyproject.toml` is invoked and the member
declares no workspace of its own, nab walks upwards from the member's
directory for the first ancestor that declares one, in either file:
`[tool.nab.workspace]` in its `pyproject.toml`, or `[workspace]` in
its `nab.toml`. The `pyproject.toml` is checked first.

Each member of the matched workspace contributes a local source. The
provider prefers those local sources over PyPI for any requirement
whose canonical name matches.

## Interaction with `[[tool.nab.local-sources]]`

Explicit `[[tool.nab.local-sources]]` entries always win. When a
workspace-discovered member shares a canonical name with an
explicit entry, the discovered entry is dropped and the override is
logged at INFO so it can be audited. Use this to point a workspace
member at a checkout outside the tree without removing it from the
`members` list.

## Build policy floor

Workspace members frequently declare `dynamic = ["version"]` or
similar. nab promotes the active `[tool.nab].build-policy` to at
least `build-local` whenever a workspace is in scope, so PEP 517
metadata extraction runs even when the user-set policy is stricter.
A policy that is already `build-local` or `build-remote` is left
alone.

## Scope of `[tool.nab]` keys

Workspace discovery flows these from the root into the file being
locked: the workspace `members` (merged into `local-sources`), and
the build-policy floor described above. Everything else under
`[tool.nab]` is scoped to the pyproject being locked: `conflicts`,
`default-groups`, `constraints`, `matrix`, `mode`, `requires-python`,
`uploaded-prior-to`, `build-policy` itself, `dist-policy`, `vcs`,
`indexes`, and `environment`. Locking a member with `nab lock
packages/core/pyproject.toml` reads only that file's keys; the
root's are ignored.

Declare conflicts and default-groups on each member that needs them.
The same applies to constraints: a root-level constraint table does
not constrain a member resolve.

## Example layout

```
/repo
├── pyproject.toml          # carries [tool.nab.workspace]
├── packages/
│   ├── core/
│   │   ├── pyproject.toml  # [project].name = "myorg-core"
│   │   └── src/...
│   └── extras/
│       ├── pyproject.toml  # [project].name = "myorg-extras"
│       └── src/...
└── tools/
    └── cli/
        ├── pyproject.toml  # [project].name = "myorg-cli"
        └── src/...
```

```toml
# /repo/pyproject.toml
[project]
name = "myorg"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "myorg-core",
    "myorg-extras",
    "myorg-cli",
]

[tool.nab.workspace]
members = [
    "packages/core",
    "packages/extras",
    "tools/cli",
]
```

`nab lock /repo/pyproject.toml` resolves the three workspace
dependencies against the in-tree directories rather than fetching
them from PyPI. Transitive dependencies of each member are still
resolved through the configured indexes.
