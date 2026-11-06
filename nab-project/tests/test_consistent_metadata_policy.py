"""The declared metadata assumption changes the real multi-target dependency graph."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_project._testing.coordinator_fake import make_coordinator
from nab_project.inputs import ResolveInputs
from nab_project.lockfile import IndexPin
from nab_project.resolve import resolve_for_targets
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_metadata, make_wheel

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("assumed", [False, True])
def test_input_policy_reaches_every_target_provider(
    tmp_path: Path, assumed: bool
) -> None:
    linux = make_wheel(tag="py3-none-manylinux_2_17_x86_64")
    windows = make_wheel(tag="py3-none-win_amd64")
    linuxdep = make_wheel(package="linuxdep")
    windep = make_wheel(package="windep")
    assert linux.metadata_url is not None
    assert windows.metadata_url is not None
    assert linuxdep.metadata_url is not None
    assert windep.metadata_url is not None
    fetch = make_coordinator(
        listings={"pkg": [linux, windows], "linuxdep": [linuxdep], "windep": [windep]},
        metadata_by_url={
            linux.metadata_url: make_metadata("pkg", "1.0", "linuxdep"),
            windows.metadata_url: make_metadata("pkg", "1.0", "windep"),
            linuxdep.metadata_url: make_metadata("linuxdep", "1.0"),
            windep.metadata_url: make_metadata("windep", "1.0"),
        },
    )
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\nname = "demo"\nversion = "1.0"\ndependencies = ["pkg"]\n'
    )
    targets = [
        ResolveTarget.for_declared(python_version="3.11", spec=PlatformSpec(platform))
        for platform in ("linux_x86_64", "windows_amd64")
    ]
    transport = HttpxAsyncTransport()
    try:
        with patch(
            "nab_project.resolve.FetchCoordinator", return_value=nullcontext(fetch)
        ):
            result = resolve_for_targets(
                path,
                transport,
                targets=targets,
                inputs=ResolveInputs(assume_consistent_metadata=assumed),
            )
    finally:
        asyncio.run(transport.aclose())
    assert len(result.target_results) == len(targets)
    assert all(target.success for target in result.target_results)
    first, second = result.target_results
    assert set(first.pins) == {"pkg", "linuxdep"}
    assert set(second.pins) == ({"pkg", "linuxdep"} if assumed else {"pkg", "windep"})
    assert second.lock is not None
    selected = second.lock.pins["pkg"]
    assert isinstance(selected, IndexPin)
    assert windows.filename in {wheel.filename for wheel in selected.wheels}
