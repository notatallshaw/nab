"""Default and explicit search order through real CLI resolution and locks."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from nab.cli import run
from nab_project import toml_io
from nab_provider.testing import make_coordinator, make_metadata, make_wheel

if TYPE_CHECKING:
    from collections.abc import Iterator

    from nab_index.transport import AsyncHttpTransport
    from nab_provider.testing import FakeFetchPort


@contextmanager
def _registry(
    transport: AsyncHttpTransport, **_settings: object
) -> Iterator[FakeFetchPort]:
    """Keep alpha resident while beta lands only when its request is awaited."""
    alpha = [
        make_wheel(str(version), package="alpha", hashes=(("sha256", "a" * 64),))
        for version in range(1, 5)
    ]
    beta = [
        make_wheel(str(version), package="beta", hashes=(("sha256", "b" * 64),))
        for version in (1, 2)
    ]
    metadata: dict[str, str] = {}
    for wheel in alpha:
        url = wheel.metadata_url
        assert url is not None
        metadata[url] = make_metadata(
            "alpha", wheel.version, "beta==2" if wheel.version == "1" else "beta==1"
        )

    for wheel in beta:
        url = wheel.metadata_url
        assert url is not None
        metadata[url] = make_metadata("beta", wheel.version)

    try:
        yield make_coordinator(
            listings={"alpha": alpha},
            pending_listings={"beta": beta},
            metadata_by_url=metadata,
        )
    finally:
        asyncio.run(transport.aclose())


def _project(directory: Path) -> Path:
    """Write a project whose valid pins depend on package decision order."""
    path = directory / "pyproject.toml"
    path.write_text(
        '[project]\nname="app"\nversion="1"\ndependencies=["alpha", "beta>=1"]\n'
        '[tool.nab]\nmode="universal"\nbuild-policy="never"\n'
        '[tool.nab.matrix]\npython="==3.12"\nplatforms=["linux_x86_64"]\n'
    )

    return path


@pytest.mark.parametrize("order", [None, "stable", "arrival"])
def test_cli_order_controls_the_valid_solution(
    hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch, order: str | None
) -> None:
    """The stable default waits for beta; explicit arrival may decide alpha first."""
    monkeypatch.setattr("nab_project.resolve.FetchCoordinator", _registry)
    path = _project(hermetic_roots)
    lock = hermetic_roots / "pylock.toml"
    arguments = [
        "--no-progress",
        "lock",
        str(path),
        "--output",
        str(lock),
        "--no-cache",
    ]
    if order is not None:
        arguments += ["--project-decision-order", order]

    assert run(arguments) == 0

    pins = {
        package["name"]: package["version"]
        for package in toml_io.load_path(lock)["packages"]
    }

    assert pins == (
        {"alpha": "4", "beta": "1"}
        if order == "arrival"
        else {"alpha": "1", "beta": "2"}
    )


def test_saved_arrival_solution_can_be_checked_and_reproduced_explicitly(
    hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checking an existing lock preserves its pins across the default change."""
    monkeypatch.setattr("nab_project.resolve.FetchCoordinator", _registry)
    path = _project(hermetic_roots)
    lock = hermetic_roots / "pylock.toml"
    arguments = [
        "--no-progress",
        "lock",
        str(path),
        "--output",
        str(lock),
        "--no-cache",
    ]

    assert run([*arguments, "--project-decision-order", "arrival"]) == 0

    before = lock.read_bytes()

    assert run([*arguments, "--locked"]) == 0
    assert lock.read_bytes() == before

    assert run([*arguments, "--reproduce"]) == 1
    assert lock.read_bytes() == before

    assert run([*arguments, "--reproduce", "--project-decision-order", "arrival"]) == 0
    assert lock.read_bytes() == before
