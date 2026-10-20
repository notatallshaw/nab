# Configuration

Project settings live in `[tool.nab]` in `pyproject.toml`, or at the top level of a
project-directory `nab.toml`. CLI overrides apply to one run.

## Find the effective value

```bash
nab config list
nab config explain resolution
```

`list` shows each option's value and winning source. `explain` also shows sources that lost. See
[Configuration sources](configuration-sources.md) for file locations, precedence and environment
variables.

## Choose a topic

[Resolution settings](resolution-settings.md): Constraints, version selection and upload cutoffs.

[Target environment](target-environment.md): Resolve for one Python and platform.

[Target matrix](target-matrix.md): Declare several Python, platform and implementation targets.

[Platform tags](platform-tags.md): Set libc, macOS and interpreter ABI requirements.

[Indexes](indexes.md): Order package indexes and select their API serialization.

[Package and index policy](package-policy.md): Apply policy to particular packages or indexes.

[Metadata overrides](metadata-overrides.md): Replace published dependencies, Python requirements or
extras.

[Distribution policy](distribution-policy.md): Choose wheels, sdists and recorded artifacts.

[Dependency sources](dependency-sources.md): Configure local checkouts, pinned git repositories and
archives.

[Configuration sources](configuration-sources.md): Find file locations, precedence and runtime
environment variables.

[Dependency selection](selection.md) covers extras and groups. [Lockfile groups](lockfile-groups.md)
explains `default-groups`, `base-group` and `build-group`. [Build policy](build-policy.md) controls
backend execution and nested build environments.

```{toctree}
---
hidden:
---
resolution-settings
target-environment
target-matrix
platform-tags
indexes
package-policy
metadata-overrides
distribution-policy
dependency-sources
configuration-sources
```
