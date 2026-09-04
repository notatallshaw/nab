# Resolution failures

What `nab lock` and `nab download` print when a resolve fails, and what
`-v` adds to it. Both commands are on the [CLI](cli.md) page.

## The failure message

`nab lock` and `nab download` exit non-zero on resolution failure; the
message starts with `error: resolution failed:` followed by a derivation
tree, and any captured diagnostics are appended under a `Diagnostics:`
section.

## The `Diagnostics:` section

Each package that ran out of versions gets one line, naming the setting
that refused its files. An indented `try:` line follows where changing a
setting would admit them again. Where the line names a config entry, it
is one of the per-package or per-index overrides in
[Configuration](configuration.md).

```
Diagnostics: (-v for detail)
  - foo: uploaded-prior-to excluded every file
    try: set packages."foo".uploaded-prior-to = false
```

`-v` replaces the `try:` line with the whole record: one clause per
cause, and a closing `note:` naming the configuration layer that set the
key.

```
Diagnostics:
  - foo: uploaded-prior-to excluded every file
    the uploaded-prior-to cutoff 2026-05-01T00:00:00+00:00 excluded 1 file uploaded at 2030-01-01T00:00:00Z (1.0)
    the files nab read hold no sdist to build from
    note: the project-level uploaded-prior-to set that cutoff; setting packages."foo".uploaded-prior-to = false lifts it for this package
```

## Worth knowing

| | |
| ---- | ------ |
| `try:` is an instruction, not a fragment to paste | The table the key belongs in usually exists already, and a second one is a TOML error. The line names the entry or table to edit. |
| Lifting a filter admits files, it does not promise a resolve | A file two filters would both refuse is charged to the first that did, so lifting the named key can uncover a second. |
| An entry covering several packages changes all of them | A `[[tool.nab.package-rules]]` entry matching two names lifts the key for both. |
| Four or more filters are counted, not named | The line reads `4 filters excluded every file`, and `-v` names them one to a clause. Three or fewer are named outright. |
| `requires-python` never gets a `try:` | The override that lifts it replaces the package's declared metadata, so the line names the target instead: `no file supports Python 3.12`. |
| An extras line can be about the base package | `foo[bar]` has versions only where `foo` does, so a filter that empties `foo`'s listing gives the line and its `try:` under `foo[bar]`, naming `foo` as the entry to edit. |
| Some lines name no setting | Nothing in the configuration produced them, such as `package not found on any configured index` or `the index lists this package but every file is yanked`. |
| A routed package is missing from one index, not from all of them | An `index` entry is a strict pin, so the line names that index: `not found on index 'internal', the only index this package is routed to`. |

## Several targets

Universal matrices and declared extra or group conflicts can produce several resolve targets. If any fails, nab writes no lock or requirements output. The failure report goes to stderr, with one labelled block per target:

```
error: resolution failed:
# py311-linux_x86_64
attrs==26.1.0
# py312-linux_x86_64: FAILED
#   ResolutionError: because no versions of attrs <1.0 are available
#   because your project depends on attrs <1.0
#   so your project's requirements cannot be satisfied
```

Successful targets show their pins; failed targets show an indented error and any `Diagnostics:` details. A failure in a conflict's base selection adds a `# base/<label>: FAILED` block; see [Conflicting selections](../explanation/conflicts.md).
