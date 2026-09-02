# Contributing

Working on nab itself, rather than installing it as a tool, starts
from the workspace check-out.

## Development install

nab uses [hatch] as its environment manager. Install it, then clone the
repository and let it do the editable installs:

```bash
git clone https://github.com/notatallshaw/nab.git
cd nab
hatch shell
nab --version
```

That shell runs in the check-out's `.venv`, with all six distributions
installed editable plus the `tests` and `nox` dependency-groups. The
commands below run tools by path, so they work outside the shell too.

Ruff and the docs toolchain live in their own hatch environments,
reached through `hatch run`.

[hatch]: https://hatch.pypa.io/

## Running the tests

The default suite covers every module under `nab_resolver`,
`nab_markersets`, `nab_provider`, `nab_project`, `nab_index`, and
`nab`:

```bash
.venv/bin/python -m pytest                # default selection (no markers)
```

Branch coverage runs through the `coverage` CLI. Each process writes its
own data file, so combine before reporting:

```bash
.venv/bin/python -m coverage erase
.venv/bin/python -m coverage run -m pytest
.venv/bin/python -m coverage combine
.venv/bin/python -m coverage report       # fails below 100 percent
```

CI checks each workspace's coverage from its own tests through nox (see
`noxfile.py`). Reproduce a single workspace, or all of them, with:

```bash
.venv/bin/nox -s tests                    # every workspace, each gated
.venv/bin/nox -s tests -- project         # just one workspace
.venv/bin/nox -s standalone               # nab-markersets on released packaging
```

`standalone` is the only run without `nab-provider` installed, so
`nab_markersets` binds released `packaging` rather than the vendored
fork.

Property-based tests are opt-in via marker:

```bash
.venv/bin/python -m pytest -m property    # Hypothesis-only suites
```

Lint and format with ruff, out of the `lint` environment:

```bash
hatch run lint:check
hatch run lint:fmt
```

Type-check through nox, which installs the pinned checker lock and runs
one checker over its own scope:

```bash
.venv/bin/nox -s "types(checker='pyright')"   # or mypy, ty, pyrefly, zuban
```

CI runs all five rather than one, so a change pyright accepts can still
fail the matrix; the trees are `TYPED_TREES` in `noxfile.py`.

## The build backend

nab builds its distributions with `--no-isolation`, so the backend must
be installed before the build. `tasks/refresh-locks.sh` locks
`[build-system].requires` into
`.github/requirements/pylock.build.toml` with
`nab lock --build-requirements`. The `dists` nox session, release
workflow, and `hatch run release:build` install that lock.

One lock serves all six packages while they declare the same
`[build-system]`. The refresh script checks that before writing, then
checks that locks sharing an environment agree on shared packages.

## Building the docs

```bash
hatch run docs:build     # sphinx-build -W, warnings are errors
hatch run docs:serve     # live-reloading preview
```

The `docs` dependency-group in `pyproject.toml` declares the doc
tooling. `tasks/refresh-locks.sh` writes it to
`.github/requirements/pylock.docs.toml`; CI and Read the Docs install
that lock. After changing the group, refresh and commit the lock.

Unlike the other groups, that lock is resolved only for Python 3.13,
which Read the Docs and the CI docs job use. nab runs on 3.10 and newer,
but locking the toolchain across that range would hold it to packages
that still support the floor.

## Coverage policy

The `pyproject.toml` `[tool.coverage.report] fail_under = 100`
setting requires 100 percent branch coverage on every workspace
package: `nab_resolver`, `nab_markersets`, `nab_provider`,
`nab_project`, `nab_index`, and `nab`.

The full local suite under `coverage run -m pytest` checks all six
together. Nox splits them per workspace in CI and runs each suite once,
appending to one coverage data file. Its result includes the workspace's
own suites plus every suite run before it.

`nab_index` and `nab_provider` are checked in the `project` workspace;
reaching every line of both takes
`nab-project/tests` as well as `nab-provider/tests`. The `provider`
workspace runs `nab-markersets/tests` and `nab-provider/tests` without
`nab-index` installed, and checks `nab_markersets`.

When code is unreachable from the default suite, prefer:

* `# pragma: no cover` for a platform-specific or defensively
  unreachable line.
* `raise RuntimeError("Bug: unreachable")` style guards: `coverage`
  excludes those automatically via the `raise RuntimeError.*unreachable`
  pattern in `[tool.coverage.report].exclude_also`.

Code under `_build/env.py` and the CLI typically mocks subprocesses,
network calls, and venv creation rather than lowering coverage.
