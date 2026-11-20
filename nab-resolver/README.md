# nab-resolver

Find a combination of package versions that satisfies their dependency constraints, using the
PubGrub algorithm. A `ResolverProvider` supplies the package rules, so the solver needs no
Python-specific knowledge. The Python provider lives in
[`nab-provider`](https://pypi.org/project/nab-provider/) and the user-facing CLI in
[`nab`](https://pypi.org/project/nab/).

It has no runtime dependencies: the standard library is all it needs.

## When to use it

Use `nab-resolver` when you are building some kind of package resolver, Python or otherwise. There
is a worked example in the docs:
<https://nab.readthedocs.io/en/stable/how-to/embed-the-resolver.html>

## The public API

This release supports the import paths below. They can change between 0.0.x releases, so pin the
version you tested.

| Module                     | Supported names                                                                                                              |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `nab_resolver.resolver`    | `Resolver`, `ResolverProvider`, `BaseProvider`, `ResolverObserver`, `Solution`, `DEFAULT_MAX_ITERATIONS`                     |
| `nab_resolver.root`        | `ROOT`                                                                                                                       |
| `nab_resolver.priority`    | `compute_tier`                                                                                                               |
| `nab_resolver.ranges`      | `Range`, `IntegerRangeMembership`                                                                                            |
| `nab_resolver.types`       | `RootRequirement`, `Term`, `Incompatibility`, `IncompatibilityCause`, `RangeProtocol`                                        |
| `nab_resolver.errors`      | `ResolutionError`, `ResolutionTerminatedError`, `ResolutionLimitError`, `ResolutionInvariantError`, `ResolutionStalledError` |
| `nab_resolver.diagnostics` | `DiagnosticMessage`, `Suggestion`, `error_message`, `suggestion_lines`                                                       |
| `nab_resolver.testing`     | `ProviderScenario`, `ProviderConformanceError`, `check_provider`, `check_provider_scenarios`                                 |

Import the names you need from their modules. Importing `nab_resolver` alone loads no submodules.
Other module paths are internal and may change in any release.

## Checking preferred versions

`try_resolve_preferred` checks a proposed assignment against the current provider's version choices,
dependencies and emitted incompatibilities. It returns the reachable pins on success, or `None` when
ordinary resolution is needed.

Requirements or constraints can force a different version from the preference. If the preferred
version remains in range but the provider chooses another, the trial refuses and leaves the choice
to ordinary search.

```python
trial = Resolver(make_provider())
pins = trial.try_resolve_preferred(requirements, preferred, constraints)
if pins is None:
    pins = Resolver(make_provider()).resolve(requirements, constraints)
```

The trial consumes its resolver and provider. Create both again before falling back; provider
callbacks may have queued facts during the trial. Nab's project layer owns these attempts for each
target and retains package names and alignment preferences between targets.
