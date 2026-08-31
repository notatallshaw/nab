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
extra, a dependency group, or one of the groups `[tool.nab]` names
itself: `base-group` for the project's own dependencies and
`build-group` for its `[build-system].requires`.

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
    { members = [{ extra = "cpu" }, { extra = "gpu" }], policy = "at-most-one" },        # ?? (default)
    { members = [{ extra = "cuda11" }, { extra = "cuda12" }], policy = "exactly-one" },  # ^^
    { members = [{ group = "a" }, { group = "b" }], policy = "at-least-one" },           # ||
]
```

A table without a `policy` key defaults to `at-most-one`, the same as
the bare-list form.

A member may appear in only one conflict set. Naming the same extra or
group in two sets is refused when the config is read, whether both sets
sit in one file or one comes from `pyproject.toml` and the other from a
project-directory `nab.toml`.

Extra and group names are normalised (PEP 685 / PEP 735), so the
spelling here does not have to match the table key exactly.

In a workspace, `conflicts` is scoped to the pyproject being locked.
Declaring conflicts in the workspace root does not propagate to a
`nab lock packages/<member>/pyproject.toml`; each member declares
its own. See [Lock a workspace](../how-to/workspaces.md) for the full
list of keys that flow vs stay local.

## Conflicting the build requirements

`[tool.nab].build-group` puts a project's `[build-system].requires` in
the same lock as its dependencies, resolved in one version space. A
backend that needs a version the project's own dependencies exclude
therefore fails to resolve. Declaring the two mutually exclusive splits
them:

```toml
[project]
dependencies = ["packaging<24"]

[build-system]
requires = ["hatchling", "packaging>=24"]

[tool.nab]
base-group = "base"
build-group = "build"
conflicts = [[{ group = "base" }, { group = "build" }]]
```

Each side now resolves on its own and gets its own pins, disjoint on the
group clause, which is what PEP 517 build isolation gives the build at
install time. `base-group` must be set: it is what makes the project's
own dependencies a named context that can be one side of a conflict.

Conflicting `build-group` against an ordinary group instead, say
`[[{ group = "build" }, { group = "dev" }]]`, separates the build
requirements from that group but not from the project's dependencies,
which stay in every fork.

A configured name is active on every run rather than selected, so a set
holding only configured names engages always: every `nab lock` for that
project forks, and `at-most-one` and `exactly-one` say the same thing
there. An `at-least-one` set that names either of them is refused, since
a group that is always active means the set can never fail.

Selecting an extra never deselects the project's own dependencies, so
pairing `base-group` with an extra is refused when the config is read,
under `at-most-one` and `exactly-one` alike. `build-group` pairs with
an extra fine: the project's own dependencies stay in every fork of
that set.

A member belongs to at most one set, so putting the build requirements
in a set with both the project's dependencies and `dev` is one
three-member declaration, and it makes all three mutually exclusive:

```toml
conflicts = [[{ group = "base" }, { group = "build" }, { group = "dev" }]]
```

That forks three ways when the run selects `dev`, and two otherwise,
since the set forks only over the members the selection activates. With
`dev` selected, an install choosing it gets that group without the
project's own dependencies. Declare two sets instead if
`dev` should install alongside them, remembering that `build` can be in
only one of them.

> [!NOTE]
> `base-group` is in the lock's `default-groups`, so an install that
> selects no group gets the project's own dependencies, and one that
> selects `build` gets the build side in their place. An installer that
> cannot select a group from a lock gets the defaults.

## Forking co-selected members

When the selection activates two or more members of an exclusive set
(`at-most-one` or `exactly-one`), nab does not reject it: it forks the
resolve. For example `nab lock --extras cpu gpu`, or
`nab lock --all-groups` over the `black*` groups above, resolves each
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
members of one exclusive set together, no fork can separate them, so the
resolve is refused before any network work:

```console
$ nab lock --extras all
error: extra 'cpu', extra 'gpu' cannot be selected together: declared mutually exclusive (at-most-one) in [tool.nab].conflicts
```

This happens when an umbrella extra self-references both members
(`all = ["proj[cpu]", "proj[gpu]"]`) or an umbrella group includes both
member groups. Co-selecting the members directly still forks. The
all-in-one umbrella cannot resolve disjointly, so it is rejected.

The require-one policies still raise. Declaring `exactly-one` or
`at-least-one` and selecting none of its members is rejected before the
resolve, regardless of mode. Co-selection forks under `exactly-one`;
`at-least-one` permits it, so its members stay active together in one
resolve.

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
error: default-groups activates 'black22', 'black23', which are declared mutually exclusive in [tool.nab].conflicts
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

An exclusive set forks only over the members the selection activates.
The rest of its declared members are absent from the lock's `extras` and
`dependency-groups` arrays and from every marker, so a set with an
unselected member gates an entry exactly as one without it would.

A dependency a member shares with a selection outside the set names
both in its marker (`"cpu" in extras or "docs" in extras`), so
selecting the member on its own still installs it.

The lockfile stays within PEP 751: the membership markers use the
standard `extras` and `dependency_groups` variables, and each fork's
marker negates the other members it was forked against
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
