# nab-project

Use `nab-project` to resolve project dependencies, build wheels from source and write lockfiles. It
fetches distributions through [`nab-index`](https://pypi.org/project/nab-index/) and applies
packaging rules through [`nab-provider`](https://pypi.org/project/nab-provider/). The API is
experimental. Pin an exact version when embedding it.

## Resolve for the current interpreter

```python
from pathlib import Path

from nab_index.urllib3_async_transport import Urllib3AsyncTransport
from nab_project.inputs import ResolveInputs
from nab_project.resolve import resolve_for_targets
from nab_provider.target import ResolveTarget


def project_versions(path: Path, inputs: ResolveInputs) -> dict[str, str]:
    result = resolve_for_targets(
        path,
        Urllib3AsyncTransport(),
        targets=[ResolveTarget.for_host()],
        inputs=inputs,
    )
    result.raise_for_failure()
    return {
        name: str(version) for name, version in result.target_results[0].pins.items()
    }
```

Call `project_versions(Path("pyproject.toml"), ResolveInputs())` to use PyPI. To supply your own
indexes, pass `ResolveInputs(indexes=...)`.

A failed target also produces a result, so call `raise_for_failure()` before using its pins.

To read Nab's configuration files, also install the matching `nab` distribution. Import
`read_pyproject_config` and `plan_targets` from `nab.config.model`, then call
`config = read_pyproject_config(path)`. Pass `inputs=config.resolve_inputs()` and
`targets=plan_targets(config)` to `resolve_for_targets`.

For the requirements behind the selected versions, pass `include_dependency_requirements=True`. Each
successful target result then exposes them through `dependency_requirements()`.
