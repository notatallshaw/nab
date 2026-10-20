# Find a feature

Start with [your first universal lock](../tutorial/getting-started.md). Use these topics when
adapting it to your project.

## Dependencies and targets

| Need                                                                    | Documentation                                                                                                  |
| ----------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Select Python versions, platforms and CPython or PyPy                   | [Target matrix](../reference/target-matrix.md), including per-implementation overrides and patch-release pins. |
| Set minimum libc or macOS versions, a PyPy ABI or free-threaded CPython | [Platform tags](../reference/platform-tags.md).                                                                |
| Resolve for one deployment environment                                  | [Target environment](../reference/target-environment.md), including `--python`.                                |
| Choose requirements by version or environment marker                    | [Choose dependencies and conditions](../how-to/choose-dependencies.md).                                        |
| Select extras and dependency groups, including default groups           | [Dependency selection](../reference/selection.md) and [lockfile groups](../reference/lockfile-groups.md).      |
| Keep incompatible extras or groups in one lock                          | [Conflicts](conflicts.md) explains selection forks and the available conflict policies.                        |
| Lock static build requirements separately or as a selectable group      | [Build requirements](../reference/selection.md) and [lockfile groups](../reference/lockfile-groups.md).        |

## Sources and resolution policy

| Need                                                                              | Documentation                                                                                                                                                  |
| --------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Use private indexes, route a package to one index, or pin HTML/JSON serialization | [Multiple indexes](../how-to/multi-index.md), [index reference](../reference/indexes.md) and [package policy](../reference/package-policy.md).                 |
| Use local or editable projects, Git commits, hashed archives or workspace members | [Local sources](../how-to/local-sources.md), [Git](../how-to/vcs.md), [archives](../how-to/archive-sources.md) and [workspaces](../how-to/workspaces.md).      |
| Choose newest, oldest or lowest-direct versions; constrain dependencies           | [Resolution settings](../reference/resolution-settings.md).                                                                                                    |
| Limit uploads by time and keep response timing out of version choices             | [Reproducible locks](../reference/reproducibility.md).                                                                                                         |
| Control wheel/sdist selection and backend execution, with scoped exceptions       | [Distribution policy](../reference/distribution-policy.md), [build policy](../reference/build-policy.md) and [package policy](../reference/package-policy.md). |
| Replace broken dependencies, Python requirements or extras metadata               | [Metadata overrides](../reference/metadata-overrides.md).                                                                                                      |
| Understand exact pins and yanked files                                            | [Yanked releases](../reference/yanking.md).                                                                                                                    |

## Output, operation and libraries

| Need                                                                                         | Documentation                                                                                                                                  |
| -------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Write pylock or requirements, including one requirements file per target                     | [Output formats](../reference/formats.md), including output templates and stdout.                                                              |
| Install locked dependencies or prepare an offline wheelhouse                                 | [Use a lock](../how-to/use-the-lock.md).                                                                                                       |
| Download artifacts after a fresh resolve                                                     | [`nab download`](../reference/cli.md) and [lockfile output](../reference/lockfile.md).                                                         |
| Inspect effective configuration, precedence and rejected settings                            | [Configuration sources](../reference/configuration-sources.md) and [`nab config`](../reference/cli.md).                                        |
| Work offline, choose a transport or inspect and clear caches                                 | [CLI options](../reference/cli.md) and [caching](../reference/cache.md).                                                                       |
| Diagnose a failure or change verbosity, progress and colour                                  | [Resolution failures](../reference/diagnostics.md) and [output controls](../reference/cli.md).                                                 |
| Embed the resolver or compare marker sets in another tool                                    | [Embedding](../how-to/embed-the-resolver.md), [marker algebra](../how-to/reason-about-markers.md) and [package responsibilities](packages.md). |
| Inspect a resolved dependency's original requirements and marker conditions programmatically | [Dependency declaration collection](packages.md) through `resolve_for_targets`.                                                                |
