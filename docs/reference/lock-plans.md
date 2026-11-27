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
plan name and [format](formats.md#--output) determine the default output filename. Set `output` to
choose a path relative to the project file. Inspect the plans with `nab config get plans`.

A plan can choose its own output format. For example, this plan writes `deploy.requirements.txt`
alongside the other plan's `pylock.ci.toml`:

```toml
[tool.nab.plans.deploy]
format = "requirements"
```

`--format` and `NAB_FORMAT` override the format for every selected plan.

To install a named selection with PDM, write the plan to PDM's project filename:

```toml
[tool.nab.plans.pdm-test]
groups = ["test"]
format = "pdm"
output = "pdm.lock"
```

```console
nab lock --plan pdm-test
pdm sync --no-self
```

To write the same test selection for Poetry, declare a second plan:

```toml
[tool.nab.plans.poetry-test]
groups = ["test"]
format = "poetry"
output = "poetry.lock"
```

```console
nab lock --all-plans
poetry sync --no-root
```

For uv, choose the dependencies to install in the plan and sync its frozen lock:

```toml
[tool.nab.plans.uv-test]
groups = ["test"]
format = "uv"
output = "uv.lock"
```

```console
nab lock --plan uv-test
uv sync --frozen
```

Plans with the same selections and settings can write different formats from one resolve. Each
output keeps its own refresh command and saved-lock checks. Different saved version preferences or
upload cutoffs can require separate resolves. Plans that resolve separately still share index
listings and package metadata when the index routes and build settings allow it. This sharing lasts
for one invocation and also works with `--no-cache`.

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
