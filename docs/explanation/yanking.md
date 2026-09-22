# Resolving yanked releases with PubGrub proxies

An exact pin can permit a yanked file, but that permission can depend on which parent version is selected. Nab must also try matching non-yanked alternatives before preparing a yanked candidate. This page explains how `YankProxyProvider` represents those conditions in PubGrub. The [selection rules](../reference/yanking.md) describe the resulting behavior.

The ordinary resolver runs first. When yanked candidates need consideration, project resolution uses `YankProxyProvider`.

## Candidates and file metadata

`YankCandidates` groups compatible files by package, version, version text and yanked status. A candidate can contain several wheels or a source archive; it is not one particular file. A release with both yanked and non-yanked files can therefore produce two candidates at the same version. The version text is retained for exact comparisons such as `===`.

The proxy provider gives each candidate an integer identifier. PubGrub can then distinguish the yanked and non-yanked choices even when their versions compare equal. `YankCandidates` retains file listings and metadata already read. Reusing that metadata does not give a candidate permission to use a yanked file.

## Two internal packages

A proxy package is an internal dependency understood by the solver, not a package that will be installed. Nab already uses extra packages to connect an extra's requirements to its base package. Yanking needs two internal package types:

- `PermissionProxy` offers reasons for allowing a yanked candidate. Each choice depends on the selected parent candidates that make an exact pin active. Root pins can provide permission directly.
- `MetadataProxy` exposes the candidate's dependencies after its metadata may be prepared. Both non-yanked and yanked candidates use it.

Suppose the project requests `app`, and selected `app==2` requires `lib==1`. The diagram shows the dependency on `lib` and its two internal packages. Arrows mean dependencies, not execution order:

```text
input requirement
        |
        v
      app==2 -----------> lib==1 (yanked candidate)
        ^                    |               |
        |                    v               v
        +------------- PermissionProxy   MetadataProxy
                       reason: app==2    dependencies of lib==1
```

The permission choice depends on `app==2`, so it cannot continue to justify `lib==1` after the solver replaces that parent. Another selected parent may supply a different reason. Once recorded, a permission choice's dependencies do not change.

The apparent cycle in the diagram is allowed because permission starts from the input requirement on `app`. Before each decision scan, the provider follows input requirements and already-permitted parents to determine what may be prepared. Two yanked candidates cannot authorize each other using only their cached metadata. The code calls permission established from an input in this way *grounded*.

If no remaining candidate can yet be prepared, the provider reports an incompatibility that depends on the current selected candidates. PubGrub may change those choices and discover another parent's pin. Reporting permanent absence instead would incorrectly exclude a candidate that becomes permitted later.

Metadata cached by an earlier resolution still enters the new solver through the metadata proxy's dependency incompatibilities. Excluding candidates using cached dependencies before adding those incompatibilities would leave PubGrub with an unexplained rejection.

## Checking non-yanked alternatives

Trying a yanked candidate after one branch fails is not enough. Suppose the project independently requests `a==1` and `b`. Non-yanked `a==1` requires `b==1`, while yanked `a==1` works with the preferred `b==2`. Failure with `b==2` does not justify the yank: the declared requirements also permit `b==1`. Another dependency may already require a yank, preventing the ordinary non-yanked solve from finishing.

Before preparing a yanked candidate with a matching non-yanked alternative, `YankPreference` starts a separate PubGrub resolution through a fresh `YankProxyProvider`. It retains the selected parents that declare requirements on the candidate, without treating the candidate's own dependencies as reasons to retain them. Independent package versions may change. Already-selected non-yanked packages must remain non-yanked, though their versions may change and optional packages may disappear.

```text
Current selection: a==1 (yanked), b==2
                         |
                         v
New resolution check: a must be non-yanked; b may change version
                         |
                         v
Complete result: a==1 (non-yanked), b==1
```

A successful check can become the final result directly. If it proves that no non-yanked alternative works under the retained requirements, the original search may continue toward a yanked candidate. Missing information or an interrupted check cannot establish that failure. The scheduler uses an explicit stack to handle further checks without recursive solver calls.

This differs from replacing a preferred parent that pins its child. For example, selected `app==2` requiring `lib==2` remains fixed while checking alternatives for `lib`. Nab need not choose older `app==1` merely because it would require a different, non-yanked `lib==1`.

Before reading yanked metadata after a failed check, another PubGrub call tests whether the current selection already contradicts known dependencies. This check treats unread metadata as unknown. It can reject a known contradiction; success does not establish a complete solution or create permission to use a yank. Admitted yanked metadata may still need to be read because it could contain another package's required exact pin.

Proxy resolutions share the candidate cache and identifiers. Each has separate decisions, permission choices and learned incompatibilities. The known-dependency check has its own provider. PubGrub selects candidates inside each of these resolutions.

## Missing data and diagnostics

Another complete alternative may succeed after a listing or metadata response is unavailable offline. When missing information prevents a justified result, resolution remains incomplete. All permission and preference checks share a budget of 100,000 PubGrub rounds; exhausting it does not prove that no solution exists.

After resolving, nab follows the selected dependencies from the inputs to recover one admitting declaration per yanked package. Warnings report the input pin or selected parent's dependency, together with the publisher's reason when available.
