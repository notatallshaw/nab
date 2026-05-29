# Conflicting extras and groups

> [!WARNING]
> Conflict declarations are tied to universal mode, which is
> experimental. The lockfile shape may change.

Some optional dependency sets are mutually exclusive: a CPU build and a
GPU build of the same stack, or testing variants pinned to different
tool versions. nab cannot put two such sets in one resolution, and a
universal lock that tried to would fail the emit-time disjointness
check. Declaring the conflict tells nab to keep them apart.

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

To require a choice rather than merely forbidding co-selection, name the
policy explicitly. The three policies mirror Gentoo's `REQUIRED_USE`
operators:

```toml
[tool.nab]
conflicts = [
    { at_most_one = [{ extra = "cpu" }, { extra = "gpu" }] },   # ?? (default)
    { exactly_one = [{ extra = "cpu" }, { extra = "gpu" }] },   # ^^
    { at_least_one = [{ group = "a" }, { group = "b" }] },      # ||
]
```

Extra and group names are normalised (PEP 685 / PEP 735), so the
spelling here does not have to match the table key exactly.

## Specific mode: fail fast

A single-environment resolve cannot serve two exclusive members at
once. Selecting both is rejected before any network work:

```console
$ nab lock --extras cpu --extras gpu
Error in [tool.nab]: extra 'cpu', extra 'gpu' cannot be selected
together: declared mutually exclusive (at_most_one) in
[tool.nab].conflicts
```

`exactly_one` additionally rejects selecting none; `at_least_one`
rejects selecting none.

## Universal mode: fork the resolve

In universal mode nab does not reject co-selected members: it forks the
resolve. When the selection activates two or more members of a set (for
example `nab lock --all-groups` over the `black*` groups above), nab
resolves each member separately and writes every result into one
lockfile. Each fork's pins carry a marker selecting that member:

```toml
[[packages]]
name = "black"
version = "22.1.0"
marker = "... and \"black22\" in dependency_groups"

[[packages]]
name = "black"
version = "23.12.0"
marker = "... and \"black23\" in dependency_groups"
```

When several sets are engaged at once, the forks are the cartesian
product across them (one member chosen per set), so `black{22,23,24}`
crossed with `isort{5,6,7}` is nine forks. Non-conflicting selections
stay active in every fork.

The require-one check is not skipped in universal mode. Declaring
`exactly_one` or `at_least_one` and selecting none of its members still
raises before the resolve, the same as in specific mode. Only the
co-selection case differs: universal mode forks instead of rejecting.

The lockfile stays within PEP 751: the membership markers use the
standard `extras` and `dependency_groups` variables, and the install
context that would activate two members of one set is pruned by the
declared conflict, so the two entries never collide for a reader. A
collision that is *not* covered by a declared conflict still raises a
`DisjointnessError`, now with a hint pointing at the `conflicts` key.

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
