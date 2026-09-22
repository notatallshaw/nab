# Yank permission in dependency resolution

A dependency's exact pin can allow a matching yanked file while its parent remains selected. Rejecting that parent removes its permission; another active pin can still allow the file. See [yanked releases](../reference/yanking.md) for the selection rules.

## Candidate search and shared metadata

The ordinary PubGrub resolve runs first. If it needs to consider yanked files, `YankResolver` searches candidate combinations with an explicit stack. A candidate identifies a package, version text and yank status, and represents the compatible files in that group. It is not an individual wheel or source archive.

`YankCandidates` stores those candidates and their prepared metadata. `SearchFrame` stores the current selection, its requirements and pins, and the alternatives still to try. Preparing metadata can reveal further dependencies and exact pins. Before rejecting a package with no permitted candidate, the search checks whether another pending dependency could still supply a pin.

Permission must follow a chain from the original inputs through already permitted parents. Cached metadata alone does not grant permission, and a cycle of yanked dependencies cannot authorize its own first metadata read. Search frames keep this traversal off Python's call stack.

## Searching for a non-yanked alternative

Before preparing a yanked candidate, the shared `YankPreference` scheduler can request another candidate search restricted to non-yanked choices for that package. It preserves selected declaring parents only when their requirements can be reached from the inputs without reading the candidate's own metadata.

Independently requested packages may change version within their requirements. Other selected non-yanked packages must still use non-yanked files if they remain required; an optional package may disappear, but cannot reappear yanked in a nested check. These restrictions avoid exchanging which independent package is yanked just to make one alternative work.

Each search starts with fresh failed and incomplete selections. Candidate metadata remains shared. A successful restricted search supplies a complete dependency solution. Requests for another search return to the scheduler, which keeps its own explicit stack rather than nesting calls to the candidate search.

```text
Ordinary PubGrub resolve
          |
          | needs to consider yanked files
          v
    YankPreference <--- request a non-yanked alternative search ---+
          |                                                      |
          +--- run or retry ---> YankResolver candidate search ---+
          |                              |
          | check current selection      | periodically check
          | for known contradictions     | known dependencies
          v                              v
     PubGrub check                  OptimisticProvider
                                         |
                                         v
                                    PubGrub check

Each candidate search uses the same YankCandidates metadata cache.
```

## Two checks that can reject known contradictions

`YankResolver.check_known_dependencies()` periodically runs PubGrub with `OptimisticProvider` to prune its candidate search. Unread metadata has no dependencies in this check. If more than one candidate remains for a version, their dependencies also stay unconstrained. Failure can establish that the dependency graph is impossible, but success does not establish a real solution.

The shared scheduler performs a different check after a non-yanked alternative search fails: it fixes the current candidate selection and checks its already known dependencies. A contradiction rejects that selection instead of prompting unnecessary yanked preparation. This check also leaves unknown metadata unconstrained.

Unknown metadata might still reveal a pin needed elsewhere, so a check that cannot reject the selection does not prove it will succeed. Missing offline data and search limits must not be treated as proof that non-yanked alternatives fail.

## Limits and diagnostics

The combined budget is 100,000 candidate-selection visits and PubGrub rounds, including retries and both kinds of contradiction check. Exhausting it reports incomplete resolution. The benchmark field `yanked_contexts` counts candidate-selection visits, including revisits; it is not a PubGrub round count.

After solving, the admitting declarations are reconstructed by following dependencies from the inputs. Warnings identify an input pin or selected parent dependency and retain the publisher's withdrawal reason.
