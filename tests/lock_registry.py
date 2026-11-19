"""Package index and project fixtures for lock command tests."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

from nab.cli import run
from nab_project import toml_io
from nab_provider.records import DistFile, IndexConfig
from nab_provider.testing import (
    FakeFetchPort,
    make_coordinator,
    make_metadata,
    make_wheel,
)
from nab_provider.yanking import append_yanked_file


class Registry:
    """A temporary lock project with an in-memory package index."""

    def __init__(self, directory: Path) -> None:
        """Write a two-target project and its initial package declarations."""
        self.project = directory / "pyproject.toml"
        self.output = directory / "pylock.toml"
        self.declarations: dict[str, dict[str, tuple[str, ...]]] = {
            "a": {"1": ("b==1",)},
            "b": {"1": ()},
        }
        self.requires_python: dict[tuple[str, str], str] = {}
        self.withdrawn: set[tuple[str, str]] = set()
        self.domain = "https://current.example"
        self.digest = "b" * 64
        self.upload_time: str | None = None
        self.coordinators: list[FakeFetchPort] = []
        self.project.write_text(
            '[project]\nname="example"\nversion="1"\nrequires-python=">=3.12,<3.13"\n'
            'dependencies=["a"]\n[tool.nab]\nbuild-policy="never"\ndecision-order="stable"\n'
            '[tool.nab.matrix]\npython=">=3.12,<3.13"\nplatforms=["linux_x86_64","windows_amd64"]\n'
        )

    def coordinator(
        self, _transport: object, **_kwargs: object
    ) -> nullcontext[FakeFetchPort]:
        """Build wheel listings and metadata from the current package declarations."""
        listings: dict[str, list[DistFile]] = {}
        metadata: dict[str, str] = {}
        for package, versions in self.declarations.items():
            wheel_name = package.replace("-", "_")
            files: list[DistFile] = []
            for version, dependencies in versions.items():
                filename = f"{wheel_name}-{version}-py3-none-any.whl"
                wheel = make_wheel(
                    version,
                    package=wheel_name,
                    url=f"{self.domain}/{filename}",
                    hashes=(("sha256", self.digest),),
                    upload_time=self.upload_time,
                )
                assert wheel.metadata_url is not None
                metadata[wheel.metadata_url] = make_metadata(
                    package,
                    version,
                    *dependencies,
                    requires_python=self.requires_python.get((package, version)),
                )
                if (package, version) in self.withdrawn:
                    files = append_yanked_file(files, wheel, reason="withdrawn")
                else:
                    files.append(wheel)
            listings[package] = files

        coordinator = make_coordinator(listings=listings, metadata_by_url=metadata)
        if self.domain != "https://current.example":
            coordinator.indexes = [IndexConfig("changed", self.domain + "/simple/")]
            for package in listings:
                coordinator.index.store_listing_index(package, "changed")

        self.coordinators.append(coordinator)

        return nullcontext(coordinator)

    def command(self, *arguments: str) -> int:
        return run(
            (
                "--no-progress",
                "lock",
                str(self.project),
                "--output",
                str(self.output),
                *arguments,
            )
        )

    def versions(self) -> dict[str, str]:
        return {
            package["name"]: package["version"]
            for package in toml_io.load_path(self.output)["packages"]
        }

    def add_release(self) -> None:
        self.declarations["a"]["2"] = ("b==2",)
        self.declarations["b"]["2"] = ()
