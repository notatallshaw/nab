# README benchmark inputs

The README figures were measured on November 7, 2026 using Nab
`f3c05aa4c679e2b637500536c4f9053b7820b851`, uv 0.13.7, Poetry 2.5.1 and PDM 2.29.6. Nab, Poetry and
PDM ran as ordinary installed Python wheels on CPython 3.15.0 with the GIL enabled. Project
interpreter selection used Python 3.12.13 before the timed command.

## Jupyter conditional-dependency check

Copy [jupyter.toml](jupyter.toml) to `pyproject.toml` in a separate directory for each tool. It pins
`jupyter==1.1.1` and `python-dateutil==2.9.0.post0`. Its Python range is `>=3.12,<3.15`; Nab's
consumer matrix is CPython 3.12, 3.13 and 3.14 on Linux x86-64, Apple Silicon macOS and Windows
x86-64.

Select the project interpreter before running these native commands. Remove the tool's existing lock
for a fresh-lock check:

| Row             | Native command                                 |
| --------------- | ---------------------------------------------- |
| Nab default     | `nab --no-progress lock`                       |
| Nab performance | `nab --no-progress lock --http-backend httpx2` |
| uv              | `uv lock`                                      |
| Poetry          | `poetry lock --no-interaction`                 |
| PDM             | `pdm lock --prod`                              |

For Nab performance, add the configuration shown in the main README. Both Nab rows use ordinary
target-by-target checks. uv uses its normal universal strategy, without an `environments` or
`fork-strategy` override. Poetry and PDM also use their native universal behavior under the shared
Python range.

Every saved lock was independently projected onto the nine consumer environments. The selected wheel
METADATA was read to check each projected dependency graph, including inactive markers and
transitive extras. All five rows retained `appnope` on macOS and `pywinpty` on Windows, with neither
selected on the other platforms. No Jupyter timing saving is claimed.

## Timing boundaries

Figures show medians of all ten native commands per tool and cache state. The clock includes process
startup, imports and the native lock command. Exports to pylock, interpreter selection,
imports/runtime diagnostics and independent saved-output validation are excluded. Tool caches were
isolated; cold starts with an empty application cache, and warm starts after an equal prime. OS
caches were not cleared.

Fresh runs have no native lock. Relock runs restore each tool's own prior lock before every command.
The performance row adds `--lock-policy reuse` for relocks; an accepted unchanged lock can be a
no-op. No remote freshness is promised by that policy. The README's warm added-dependency slowdown
uses the current source and settings, separately from the unchanged scientific relock.
