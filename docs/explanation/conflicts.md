# Conflicting extras and groups

> [!WARNING]
> The lockfile shape for conflicts may still change.

Some optional dependency sets are mutually exclusive: a CPU build and a
GPU build of the same stack, or testing variants pinned to different
tool versions. nab cannot put two such sets in one resolution, and a
lock that tried to would fail the emit-time disjointness check.
Declaring the conflict tells nab to keep them apart.

## Declaring a conflict

Conflicts live in `[tool.nab].conflicts`. Each entry is a set of
members that are mutually exclusive with each other. A member is an
extra or a dependency group:

```toml
[tool.nab]
conflicts = [
    [{ extra = "cpu" }, { extra = "gpu" }],
]
```

The bare list form means *at most one* of the members may be active, so
selecting neither is fine (extras are opt-in). A set may mix extras and
groups, and you can declare several independent sets:

```toml
[tool.nab]
conflicts = [
    [{ group = "black22" }, { group = "black23" }, { group = "black24" }],
    [{ group = "isort5" }, { group = "isort6" }, { group = "isort7" }],
]
```

To require a choice rather than merely forbidding co-selection, use the
table form and name the policy as a value. The three policies mirror
Gentoo's `REQUIRED_USE` operators:

```toml
[tool.nab]
conflicts = [
    { members = [{ extra = "cpu" }, { extra = "gpu" }], policy = "at-most-one" },   # ?? (default)
    { members = [{ extra = "cpu" }, { extra = "gpu" }], policy = "exactly-one" },   # ^^
    { members = [{ group = "a" }, { group = "b" }], policy = "at-least-one" },      # ||
]
```

A table without a `policy` key defaults to `at-most-one`, the same as
the bare-list form.

Extra and group names are normalised (PEP 685 / PEP 735), so the
spelling here does not have to match the table key exactly.

In a workspace, `conflicts` is scoped to the pyproject being locked.
Declaring conflicts in the workspace root does not propagate to a
`nab lock packages/<member>/pyproject.toml`; each member declares
its own. See [Lock a workspace](../how-to/workspaces.md) for the full
list of keys that flow vs stay local.

## Forking co-selected members

When the selection activates two or more members of a set, nab does not
reject it: it forks the resolve. For example `nab lock --extras cpu gpu`,
or `nab lock --all-groups` over the `black*` groups above, resolves each
member separately and writes every result into one lockfile. This is the
same in specific and universal mode; the resolve mode does not change how
a conflict is handled. Two cases are refused rather than forked, both
covered below: a selection that reaches both members through one
umbrella, and a `default-groups` that activates two members on its own.

Each fork's pins carry a marker selecting that member:

```toml
[[packages]]
name = "black"
version = "22.1.0"
marker = "... and \"black22\" in dependency_groups and \"black23\" not in dependency_groups and \"black24\" not in dependency_groups"

[[packages]]
name = "black"
version = "23.12.0"
marker = "... and \"black23\" in dependency_groups and \"black22\" not in dependency_groups and \"black24\" not in dependency_groups"
```

The requirements formats name a fork with the `{selection}` variable in
an `--output` template (`--output 'req-{selection}.txt'` writes
`req-extra-cpu.txt` and `req-extra-gpu.txt`), since two forks of one
tuple share every other axis and would otherwise collide onto one file.

When several sets are engaged at once, the forks are the cartesian
product across them (one member chosen per set), so `black{22,23,24}`
crossed with `isort{5,6,7}` is nine forks. Non-conflicting selections
stay active in every fork.

Forking needs one member per fork. If a single selection forces two
members of one set together, no fork can separate them, so the resolve
is refused before any network work:

```console
$ nab lock --extras all
error: in [tool.nab]: extra 'cpu', extra 'gpu' cannot be selected together: declared mutually exclusive (at-most-one) in [tool.nab].conflicts
```

This happens when an umbrella extra self-references both members
(`all = ["proj[cpu]", "proj[gpu]"]`) or an umbrella group includes both
member groups. Co-selecting the members directly still forks. The
all-in-one umbrella cannot resolve disjointly, so it is rejected.

The require-one policies still raise. Declaring `exactly-one` or
`at-least-one` and selecting none of its members is rejected before the
resolve, regardless of mode; co-selection forks instead.

Groups named in `[tool.nab].default-groups` count as part of the
selection for every conflict check. A project with
`default-groups = ["a"]` and a `policy = "exactly-one"` set over
groups `a` and `b`
satisfies the minimum without passing `--groups`, and a `--groups b`
on top of that default activates two members of an exclusive set,
which then forks into two.

Defaults have one restriction of their own. Naming two members of the
same `at-most-one` or `exactly-one` set as defaults is refused when the
config is read, before any command-line selection applies:

```console
$ nab lock
error: in [tool.nab]: default-groups activates 'black22', 'black23', which are declared mutually exclusive in [tool.nab].conflicts
```

A default install activates every default group at once. The emit-time
disjointness check prunes any context that activates two members of an
exclusive set, so that install is never checked against the declared
conflict. `at-least-one` permits co-selection, so all of its members
may be defaults.

A dependency required by every member of a set but not by the base
keeps its membership marker, so it does not install when no member is
selected (relevant under `at-most-one`, which permits selecting none).
A base resolve names the deps that install regardless of the
selection, which is how the writer tells the two apart. When the
forks of one environment pin a base dependency at different versions,
no single entry can serve the no-member context; the writer raises a
`DivergentBaseDependencyError` rather than emit a lock that silently
skips the dependency.

With two or more sets engaged, each entry names only the sets its
package varies over. A dependency one member of one set pulls in at the
same version in every fork of the other sets contributes no clause for
them, so selecting that member on its own installs it, and a dependency
a member of each of two sets reaches is selected by either alone. A
package whose version does depend on the combination keeps the
conjunction, and so does anything that requires it: an entry never
fires where one of its own dependencies would not.

A dependency a member shares with a selection outside the set names
both in its marker (`"cpu" in extras or "docs" in extras`), so
selecting the member on its own still installs it.

The lockfile stays within PEP 751: the membership markers use the
standard `extras` and `dependency_groups` variables, and each fork's
marker negates the co-members of every conflict set it draws from
(`"cpu" in extras and "gpu" not in extras`), so the forks are mutually
exclusive in the markers themselves. A PEP 751 consumer that never
reads `[tool.nab].conflicts` still installs at most one fork; the two
entries cannot collide for a reader. A collision that is *not* covered
by a declared conflict still raises a `DisjointnessError`, with a hint
pointing at the `conflicts` key.

## Trade-offs

| Property | nab (matrix fork) | uv (conflict markers) |
| --- | --- | --- |
| Lockfile encoding | Standard PEP 751 `"x" in extras`. | Bespoke `extra-<n>-<pkg>-<name>` dialect in `uv.lock`. |
| Mechanism | Each member is a separate resolution under a marker. | Synthetic activation variables fork the single solve. |
| Cost | Re-resolves a near-identical universe per fork. | Shared until a fork diverges. |

The cost row reflects how the matrix model works (see
[universal resolution](universal.md)): it re-resolves per fork. In
return the lock stays within PEP 751, with no custom conflict-marker
grammar.
