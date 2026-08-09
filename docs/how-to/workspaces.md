# Lock a workspace

> [!WARNING]
> Workspace support is experimental. Discovery rules and member
> validation may change.

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

## Members lock as editable installs

Each member is recorded as an editable install by default, matching
uv. Its pin renders as a PEP 660 editable entry: `editable = true`
in `pylock.toml`, and `-e file://...` in the two requirements
formats. An explicit `[[tool.nab.local-sources]]` entry defaults the
other way, non-editable, and becomes editable only when its
`editable` key is set to `true`.

## Interaction with `[[tool.nab.local-sources]]`

Explicit `[[tool.nab.local-sources]]` entries always win. When a
workspace-discovered member shares a canonical name with an
explicit entry, the discovered entry is dropped and the override is
logged at INFO so it can be audited. Use this to point a workspace
member at a checkout outside the tree without removing it from the
`members` list.

## Build policy

Workspace members frequently declare `dynamic = ["version"]` or
similar, which nab can only read by running the PEP 517 local
backend. The default `build-policy` is `build-local`, so that build
runs without any extra configuration.

A workspace does not raise the policy. Under `build-policy = "never"`
a member with dynamic metadata and no static fallback cannot be read,
and the lock fails naming it. Give every member static metadata to
lock a workspace with no backend invocations.

## Scope of `[tool.nab]` keys

Workspace discovery flows these from the root into the file being
locked: the workspace `members`, merged into `local-sources`. Everything
else under `[tool.nab]` is scoped to the pyproject being locked: `conflicts`,
`default-groups`, `constraints`, `matrix`, `mode`, `requires-python`,
`uploaded-prior-to`, `build-policy` itself, `dist-policy`, `vcs`,
`indexes`, `base-group`, and `environment`. Locking a member with `nab lock
packages/core/pyproject.toml` reads only that file's keys; the
root's are ignored.

Declare conflicts and default-groups on each member that needs them.
The same applies to constraints: a root-level constraint table does
not constrain a member resolve.

`base-group` follows the same rule: the root's setting names the root's
own dependencies in the root's lock, and a member's lock is unaffected.
Members are ordinary packages there, so the gate covers everything the
root's `[project.dependencies]` pull in, members and other local sources
alike. An install asked for `dev` alone no longer gets them, and one
that wants both asks for both. A root that declares members but has no
dependencies of its own has nothing to name.

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
