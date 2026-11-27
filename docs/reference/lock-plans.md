# Named dependency locks

Use a lock plan when contributors or CI need the same dependency environment repeatedly. A plan
writes one file. Installing that file installs its selected dependencies on a supported interpreter
and platform.

For example, a project with a `test` dependency group and an `http` extra can lock both:

```toml
[tool.nab.plans.ci]
groups = ["test"]
extras = ["http"]
```

```console
nab lock --plan ci
python -m pip install -r pylock.ci.toml
nab lock --plan ci --locked
```

Install the project itself separately when needed. To choose groups or extras at install time, use
ordinary [group selection](selection.md) when generating the lock. Use separate named files for
sessions that require incompatible versions or different target environments.

## Choose requirements and settings

Plans include runtime dependencies by default. Set `dependencies = false` for a tools-only
environment. Setting `build-requirements = true` selects static `[build-system].requires` and omits
runtime dependencies unless you also set `dependencies = true`. Either kind of plan can include
local groups and extras.

```toml
[tool.nab.plans.release]
build-requirements = true
groups = ["release-tools"]

[tool.nab.plans.release.settings]
mode = "specific"
dist-policy = "wheel-only"

[tool.nab.plans.release.settings.environment]
python = "3.14"
platform = "linux_x86_64"
```

A plan inherits the project's resolution settings. Its `settings` table overrides those settings,
and explicit CLI project settings take precedence. The plan specifies its own groups, extras and
build inputs. Project default groups apply to ordinary locks. To change a named lock's selection,
edit the plan.

Declare plans in `[tool.nab.plans.NAME]` in `pyproject.toml`, or `[plans.NAME]` in `nab.toml`. The
default output is `pylock.NAME.toml` or `NAME.requirements.txt` for a requirements format. Set
`output` to choose a path relative to the project file. Inspect the plans with
`nab config get plans`.

A plan can choose its own output format. For example, this plan writes `deploy.requirements.txt`
alongside the other plan's `pylock.ci.toml`:

```toml
[tool.nab.plans.deploy]
format = "requirements"
```

`--format` and `NAB_FORMAT` override the format for every selected plan.

Use `nab lock --all-plans` to refresh every declared plan and `nab lock --all-plans --locked` to
check the files. A batch checks the option names and values in every plan’s `settings` table,
verifies that its input sources are declared and checks for duplicate output paths before writing
the first lock. Both the individual and batch commands support `--upgrade` and `--reproduce`. To
select a default plan in user configuration, set `plan = "ci"` or `NAB_PLAN=ci`. `--all-plans`
overrides that default. On the command line, choose either `--all-plans` or a single plan.

## Import requirements from another project

Give each source directory or hash-pinned archive a name. Each plan chooses the declarations to read
from it. This example adds another project's test group alongside local tools:

```toml
[tool.nab.input-sources.shared]
path = "../shared-tools"

[tool.nab.plans.checks]
groups = ["tools"]
inputs.shared = { groups = ["test"] }
```

A different plan can select other groups from `shared`. For an archive, use `url` and `sha256` in
place of `path`. Local paths start beside the project file. Each input accepts `groups`, `extras`,
`dependencies`, `build-requirements` and `requirements-files`. To select several projects from a
single source tree, use its `projects` array.

The source contributes requirements. Install its distribution separately if the session runs its
code. A saved-lock check rereads local declarations. For remote declarations it needs the verified
files from the cache or access to the pinned archive. See
[dependency inputs](../how-to/dependency-inputs.md) for the supported formats.
