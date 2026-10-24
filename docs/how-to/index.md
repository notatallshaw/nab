# Guides

Start with [your first universal lock](../tutorial/getting-started.md), then adapt it to your
project.

## Choose dependencies and targets

- [Select requirements, markers, extras and groups](choose-dependencies.md).
- [Change Python versions and platforms](lock-multiple-environments.md).
- [Lock incompatible extras and groups](../explanation/conflicts.md).
- [Choose version preferences and constraints](../reference/resolution-settings.md).

## Use other package sources

- [Configure package indexes](multi-index.md).
- [Use a local checkout](local-sources.md).
- [Pin a Git dependency](vcs.md).
- [Use a hash-pinned archive](archive-sources.md).
- [Lock a workspace](workspaces.md).

## Use and maintain a lock

- [Install from a lock or prepare an offline wheelhouse](use-the-lock.md).
- [Write requirements for each target](../reference/formats.md).
- [Control wheel selection and builds](../reference/configuration.md).
- [Keep lock generation repeatable](../reference/reproducibility.md).
- [Troubleshoot a failed resolve](../reference/diagnostics.md).

## Use Nab in another tool

- [Embed the resolver](embed-the-resolver.md).
- [Compare and simplify marker sets](reason-about-markers.md).
- [Understand the packages and their APIs](../explanation/packages.md).

For installation options, see [Install nab](install.md). For flags and settings, use the
[CLI](../reference/cli.md) and [configuration reference](../reference/configuration.md).

```{toctree}
---
hidden:
---
install
choose-dependencies
lock-multiple-environments
multi-index
local-sources
vcs
archive-sources
workspaces
use-the-lock
embed-the-resolver
reason-about-markers
```
