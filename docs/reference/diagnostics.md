# Resolution failures

What `nab lock` and `nab download` print when a resolve fails, and what `-v` adds to it. Both
commands are on the [CLI](cli.md) page.

## The failure message

`nab lock` and `nab download` exit non-zero on resolution failure; the message starts with
`error: resolution failed:` followed by a derivation tree, and any captured diagnostics are appended
under a `Diagnostics:` section.

## The `Diagnostics:` section

Each package that ran out of versions gets one line, naming the setting that refused its files. An
indented `try:` line follows where changing a setting would admit them again. Where the line names a
config entry, it is one of the per-package or per-index overrides in
[Configuration](configuration.md).

```
Diagnostics: (-v for detail)
  - foo: uploaded-prior-to excluded every file
    try: set packages."foo".uploaded-prior-to = false
```

`-v` replaces the `try:` line with the whole record: one clause per cause, and a closing `note:`
naming the configuration layer that set the key.

```
Diagnostics:
  - foo: uploaded-prior-to excluded every file
    the uploaded-prior-to cutoff 2026-05-01T00:00:00+00:00 excluded 1 file uploaded at 2030-01-01T00:00:00Z (1.0)
    the files nab read hold no sdist to build from
    note: the project-level uploaded-prior-to set that cutoff; setting packages."foo".uploaded-prior-to = false lifts it for this package
```

## Interpreting a diagnostic

### Edit the existing configuration table

A `try:` line identifies the setting to change. Edit its existing table; repeating a TOML table is
an error. For the cutoff example above:

```toml
[tool.nab.packages.foo]
uploaded-prior-to = false
```

An entry in `[[tool.nab.package-rules]]` can cover several packages. Changing it affects every
matching package.

### More than one filter can apply

Lifting a filter admits files but does not guarantee a solution. Nab reports each file under the
first filter that excludes it; removing that filter may expose another.

Four or more causes are counted, for example `4 filters excluded every file`. Use `-v` to see each
cause separately.

### Python compatibility

`requires-python` gets no `try:` suggestion because overriding it replaces published metadata. The
diagnostic names the target instead, for example `no file supports Python 3.12`. Check the
[target environment](target-environment.md) before changing metadata.

### Extras

A diagnostic for `foo[bar]` can describe a filter on `foo` itself. The extra can only use versions
of its base package, so its `try:` line identifies the base package's policy entry.

### Package lookup

- `package not found on any configured index` means no consulted index listed the package.
- `not found on index 'internal', the only index this package is routed to` means a strict package
  route prevented looking elsewhere.
- `the index lists this package but every file is yanked` names an index condition, not a
  configuration setting.

## Several targets

Universal matrices and declared extra or group conflicts can produce several resolve targets. If any
fails, nab writes no lock or requirements output. The failure report goes to stderr, with one
labelled block per target:

```
error: resolution failed:
# py311-linux_x86_64
attrs==26.1.0
# py312-linux_x86_64: FAILED
#   ResolutionError: because no versions of attrs <1.0 are available
#   because your project depends on attrs <1.0
#   so your project's requirements cannot be satisfied
```

Successful targets show their pins; failed targets show an indented error and any `Diagnostics:`
details. A failure in a conflict's base selection adds a `# base/<label>: FAILED` block; see
[Conflicting selections](../explanation/conflicts.md).
