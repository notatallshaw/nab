"""Artifact fixtures for lock construction, selection and download tests."""

from __future__ import annotations

__lazy_modules__ = ("nab_project.lockfile",)

from nab_project.lockfile import SdistArtifact, WheelArtifact


def make_wheel_artifact(
    package: str = "foo",
    version: str = "1.0",
    *,
    tag: str = "py3-none-any",
    url: str | None = None,
    sha256: str = "a" * 64,
    size: int = 1024,
) -> WheelArtifact:
    """Build a lock wheel with a derived filename and a declared SHA-256."""
    filename = f"{package}-{version}-{tag}.whl"
    return WheelArtifact(
        filename=filename,
        url=f"https://example.com/{filename}" if url is None else url,
        hashes=(("sha256", sha256),),
        size=size,
    )


def make_sdist_artifact(
    package: str = "foo",
    version: str = "1.0",
    *,
    url: str | None = None,
    sha256: str = "b" * 64,
    size: int = 2048,
) -> SdistArtifact:
    """Build a lock sdist with a derived filename and a declared SHA-256."""
    filename = f"{package}-{version}.tar.gz"
    return SdistArtifact(
        filename=filename,
        url=f"https://example.com/{filename}" if url is None else url,
        hashes=(("sha256", sha256),),
        size=size,
    )
