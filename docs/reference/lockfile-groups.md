# Lockfile extras and groups

Nab records the extras and groups a resolve covers and adds selection markers to packages they
reach. The
[pylock specification](https://packaging.python.org/en/latest/specifications/pylock-toml/) defines
how installers evaluate those selections and `default-groups`.

Use `--extras` and `--groups` to choose what to resolve. See
[Choose dependencies and conditions](../how-to/choose-dependencies.md) for examples and
[Dependency selection](selection.md) for flags.

## Selection markers

A package reached only through `--extras cli` carries a `"cli" in extras` condition. A package
reached through several selections carries their alternatives. Packages reached by regular project
dependencies are unconditional unless `base-group` is configured.

In a matrix, Nab combines these conditions with the targets on which each package is needed.
Selecting extras and groups normally makes one joint resolve.
[Declared conflicts](../explanation/conflicts.md) let incompatible selections resolve separately
within one lock.

## Runtime dependency group

Set `base-group` to let an installer select a group without also installing the project's regular
dependencies:

```toml
[tool.nab]
base-group = "base"
```

Nab records the name in `dependency-groups` and conditions those dependencies on its selection. With
no explicit `default-groups`, it also makes `base` the default.

An explicit `default-groups` replaces that implicit default. Include `base` when it should accompany
another default group:

```toml
[tool.nab]
base-group = "base"
default-groups = ["base", "dev"]
```

Without `base-group`, regular dependencies install under every selection. A package reached by both
the base and another group carries both selection alternatives.

## Build requirements group

Set `build-group` to include the project's static `[build-system].requires` alongside its runtime
dependencies:

```toml
[tool.nab]
base-group = "base"
build-group = "build"
```

Nab adds `build` to `dependency-groups` and conditions the build requirements on it. They install by
default only if `default-groups` explicitly includes `build`. `base-group` is required so an
installer can select the two sets separately.

Only the static list is covered. Nab does not invoke the project's backend to collect additional
build requirements. A missing `[build-system]` is an error.

The build and runtime requirements share one version space. Declare a conflict between their group
names when they need incompatible versions; Nab then resolves them separately.

## Names and installer support

`base-group` and `build-group` must differ from each other and from declared dependency-group names.
These synthetic names can appear in `default-groups`, but `--groups` accepts only declared project
groups.

The requirements formats carry no selection markers, so they emit build requirements as ordinary
pins. Use `nab lock --build-requirements` to write a separate build lock when the consumer cannot
select groups. pip currently selects default groups and no extras from pylock; see
[Use a lock](../how-to/use-the-lock.md).
