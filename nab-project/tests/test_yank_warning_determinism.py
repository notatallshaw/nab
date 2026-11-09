"""Sibling-extra yanked warnings stay stable across process hash seeds."""

from __future__ import annotations

import json
import os
import subprocess
import sys

_PROBE = """
import io
import json
import logging

from nab_project._testing.yanking import graph_port
from nab_project.inputs import ResolveInputs
from nab_project.lockfile import render_lock
from nab_project.resolve import build_lock_input, resolve_with_coordinator
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider.policy import BuildPolicy, DecisionOrder
from nab_provider.tags import PlatformSpec
from nab_provider.target import Matrix

port = graph_port(
    [
        ("alpha", "1", True, ['bravo==1; extra == "x"', 'bravo==1; extra == "y"']),
        ("bravo", "1", True, []),
        ("zulu", "1", False, ["alpha==1"]),
    ],
    provided_extras={("alpha", "1", True): ("x", "y")},
)
stream = io.StringIO()
logger = logging.getLogger("nab_project._resolve.engine")
logger.addHandler(logging.StreamHandler(stream))
logger.setLevel(logging.WARNING)

inputs = ResolveInputs(
    build_policy=BuildPolicy.NEVER, decision_order=DecisionOrder.STABLE
)
targets = Matrix(python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)).expand()
result = resolve_with_coordinator(
    port,
    targets,
    [Requirement(req) for req in ("alpha[x]==1", "alpha[y]", "zulu")],
    inputs=inputs,
)

assert result.success, [str(target.error) for target in result.target_results]
assert {
    name: str(version) for name, version in result.target_results[0].pins.items()
} == {
    "alpha": "1",
    "bravo": "1",
    "zulu": "1",
}

print(
    json.dumps(
        {
            "warnings": stream.getvalue(),
            "lock": render_lock(build_lock_input(result, inputs=inputs)),
        }
    )
)
"""


def test_sibling_extra_yank_warnings_ignore_hash_seed() -> None:
    outputs = []
    for seed in (0, 1):
        result = subprocess.run(  # noqa: S603 - fixed interpreter and inline probe
            [sys.executable, "-B", "-c", _PROBE],
            env=dict(
                os.environ,
                PYTHONHASHSEED=str(seed),
                PYTHONPATH=os.pathsep.join(sys.path),
            ),
            capture_output=True,
            check=True,
            timeout=30,
        )
        outputs.append(json.loads(result.stdout))

    assert outputs[0]["lock"] == outputs[1]["lock"]
    assert outputs[0]["warnings"] == outputs[1]["warnings"]

    warnings = outputs[0]["warnings"].splitlines()
    assert warnings[0] == (
        "Selected yanked files for alpha==1: withdrawn "
        "(admitted by input pin alpha[x]==1)"
    )

    grounded_bravo_warnings = {
        "Selected yanked files for bravo==1: withdrawn "
        f'(admitted by alpha[{extra}]==1 requires bravo==1; extra == "{extra}")'
        for extra in ("x", "y")
    }
    assert len(warnings) == 2
    assert warnings[1] in grounded_bravo_warnings
