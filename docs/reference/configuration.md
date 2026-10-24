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

## Dependencies and resolution

- [Select extras and groups](selection.md).
- [Choose versions and constrain dependencies](resolution-settings.md).
- [Configure lockfile groups](lockfile-groups.md).

## Target environments

- [Resolve for one environment](target-environment.md).
- [Declare a target matrix](target-matrix.md).
- [Set platform and ABI requirements](platform-tags.md).

## Sources and policy

- [Configure indexes](indexes.md) or [direct sources](dependency-sources.md).
- [Apply package and index policies](package-policy.md).
- [Choose wheels and sdists](distribution-policy.md).
- [Control backend execution](build-policy.md).
- [Replace published metadata](metadata-overrides.md).

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
