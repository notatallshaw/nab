"""Installation contexts shared by native project lock formats."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nab_markersets.markersets import MarkerSet
from nab_provider.records import select_artifact_hash

from .pylock import LockValidationError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from packaging.markers import Marker
    from packaging.pylock import Package, PackageSdist, PackageWheel


def distribution_files(package: Package) -> Iterator[PackageWheel | PackageSdist]:
    """Yield the wheels and source archive available for an index release."""
    yield from package.wheels or ()
    if package.sdist is not None:
        yield package.sdist


def file_digest(name: str, hashes: Mapping[str, str]) -> str:
    """Format the native installer's algorithm-prefixed artifact digest."""
    digest = select_artifact_hash(tuple(hashes.items()))
    if digest is None:
        msg = f"{name} needs a SHA-256, SHA-384 or SHA-512 digest"
        raise LockValidationError(msg)

    algorithm, value = digest
    return f"{algorithm}:{value}"


def install_contexts(
    marker: Marker | None,
    *,
    base: str,
    groups: tuple[str, ...],
    extras: tuple[str, ...],
) -> Iterator[tuple[str, str, str | None]]:
    """Bind one selection at a time and retain its environment condition."""
    if marker is None:
        yield "group", base, None
        return

    condition = MarkerSet.from_marker(marker)
    for kind, variable, names in (
        ("group", "dependency_groups", groups),
        ("extra", "extras", extras),
    ):
        for name in names:
            selection = {
                "extras": frozenset[str](),
                "dependency_groups": frozenset[str](),
            }
            selection[variable] = frozenset((name,))

            # Keep the environment condition after binding the selector.
            residual = condition.restrict(selection)
            if not residual.is_empty():
                yield kind, name, residual.to_marker_string()
