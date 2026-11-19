# Read dependency inputs from source projects

The experimental `nab-project` API reads dependency declarations from local trees or source
archives. Use it to share dependency groups, collect requirements for optional features, or combine
static build requirements with other tools. Multiple projects can share one source tree; references
between them expand selected extras instead of pulling another copy from an index.

```python
from collections.abc import Callable
from pathlib import Path

from nab_project.dependency_sources import DependencySourceReader


def read_group_requirements(
    url: str,
    sha256: str,
    groups: list[str],
    cache: Path,
    fetch: Callable[[str], bytes],
    *,
    offline: bool = False,
) -> list[str]:
    sources = DependencySourceReader(Path.cwd(), cache, fetch, offline=offline)
    return sources.requirements({"url": url, "sha256": sha256}, {"groups": groups})
```

The caller's `fetch` function owns HTTP policy and returns the archive bytes. Downloads are checked
against their declared SHA-256 hashes before any declarations are cached. The reader reuses those
declarations offline without storing or extracting the whole source tree. Imported configuration and
build backends are not executed.

A single enclosing directory becomes the archive's source root. File and project paths are relative
to that root.

Use `path` instead of `url` and `sha256` for a local tree. For a single project, put the selection
fields directly in the second argument. Use `projects` for several projects in one tree. Each entry
accepts a source-relative `path` and selects requirements:

- With no category selected, import runtime dependencies.
- `groups` and `extras` select only those group or extra requirements.
- `build-requirements = true` selects static backend requirements.
- `dependencies = true` includes runtime dependencies alongside other selections.

These categories can be combined. A group-only entry needs no `[project]` table and does not read
runtime or build metadata. A requirements-file input needs no project entry or `pyproject.toml`.

`requirements-files` accepts source-relative files containing PEP 508 requirements, comments and
relative `-r` or `--requirement` includes. A `#` at the start of a line or after whitespace starts a
comment. URL fragments such as `#sha256=…` stay part of the requirement. Other pip options are not
supported. Runtime dependencies declared dynamic are refused when selected; local direct references
need an explicit source policy in the consuming project.

Inputs contribute requirements rather than adding the source projects themselves as install
candidates. When runtime dependencies or extras are selected, references among the selected source
projects expand their extras instead of requesting indexed releases. The caller supplies those
projects and checks their version compatibility.
