"""Project previous PEP 751 versions onto current resolution contexts."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

from packaging.pylock import (
    Pylock,
    PylockSelectError,
    PylockValidationError,
)
from packaging.utils import canonicalize_name

from nab_provider.environment import marker_environment
from nab_provider.tags import TagSet, parse_wheel_tag_set

from . import toml_io
from .paths import path_state

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

    from packaging.tags import Tag
    from packaging.version import Version

    from nab_provider.target import ResolveTarget

__all__ = [
    "LockPreferences",
    "SavedPreferences",
    "read_lock_preferences",
    "read_lockfile_packages",
]


class SavedPreferences(Protocol):
    """Prior version choices a fresh resolve can try under its current policy."""

    @property
    def known_packages(self) -> frozenset[str]:
        """Package names available to selective upgrades."""
        ...

    def versions_for(
        self,
        target: ResolveTarget,
        *,
        selection: Collection[tuple[str, str]] | None = None,
        default_groups: Collection[str] | None = None,
    ) -> dict[str, Version]:
        """Return candidate hints for the requested resolver target."""
        ...

    def summary_versions(self) -> dict[str, Version] | None:
        """Return stable previous versions when each name has a unique pin."""
        ...


class LockPreferences:
    """Project a validated lock's versions into current resolution contexts."""

    __slots__ = ("_lock", "_selection_lock", "_tags", "known_packages")

    def __init__(self, lock: Pylock, *, exclude: Collection[str] = ()) -> None:
        """Collect tags and exempt selected names from prior version preferences."""
        self._lock = lock
        self.known_packages = frozenset(
            str(package.name)
            for package in lock.packages
            if package.version is not None
        )

        self._selection_lock = lock
        if exclude:
            excluded = {canonicalize_name(name) for name in exclude}
            # An exempt package's old artifact can no longer veto the selection.
            self._selection_lock = replace(
                lock,
                packages=tuple(
                    package for package in lock.packages if package.name not in excluded
                ),
            )

        advertised: set[Tag] = set()
        for package in self._selection_lock.packages:
            for wheel in package.wheels or ():
                advertised.update(parse_wheel_tag_set(wheel.filename) or ())
        self._tags = tuple((tag, frozenset((tag,))) for tag in advertised)

    def summary_versions(self) -> dict[str, Version] | None:
        """Return prior pins when each name has a single recorded version."""
        return _summary_versions(self._lock)

    def versions_for(
        self,
        target: ResolveTarget,
        *,
        selection: Collection[tuple[str, str]] | None = None,
        default_groups: Collection[str] | None = None,
    ) -> dict[str, Version]:
        """Select the old lock's known members under the actual current target."""
        tags = target.tags
        if not target.tags_faithful or type(tags) is not TagSet:
            return {}

        members = target.selection if selection is None else selection
        extras = {
            canonicalize_name(name) for kind, name in members if kind == "extra"
        } & set(self._lock.extras or ())
        defaults = (
            self._lock.default_groups if default_groups is None else default_groups
        )
        groups = {
            *(canonicalize_name(name) for name in defaults or ()),
            *(canonicalize_name(name) for kind, name in members if kind == "group"),
        } & {
            *(self._lock.dependency_groups or ()),
            *(self._lock.default_groups or ()),
        }

        # Only advertised tags matter to version hints; their preference order does not.
        compatible = tuple(tag for tag, group in self._tags if tags.accepts_tags(group))
        try:
            selected = self._selection_lock.select(
                environment=marker_environment(target.marker_env),
                tags=compatible,
                extras=extras,
                dependency_groups=groups,
            )
            return {
                str(package.name): package.version
                for package, _artifact in selected
                if package.version is not None
                and package.vcs is None
                and package.directory is None
                and package.archive is None
            }
        except (
            PylockSelectError,
            ValueError,
            KeyError,
        ):
            return {}


def _summary_versions(lock: Pylock) -> dict[str, Version] | None:
    """Collect the single prior version of each package, if one exists."""
    versions: dict[str, Version] = {}
    for package in lock.packages:
        if package.version is None:
            continue
        name = str(package.name)
        if versions.setdefault(name, package.version) != package.version:
            return None
    return versions


def _read_prior_lock(path: Path) -> Pylock | None:
    """Read a previous lock for optional hints or the change summary."""
    if not path_state(path).should_read:
        return None

    try:
        with path.open("rb") as stream:
            return Pylock.from_dict(toml_io.load(stream))
    except (OSError, ValueError, RecursionError, PylockValidationError):
        return None


def read_lock_preferences(
    path: Path, *, exclude: Collection[str] = (), native_format: str | None = None
) -> SavedPreferences | None:
    """Read optional prior version hints, refusing an unreadable or invalid lock."""
    if native_format is not None:
        from ._lockfile.native_saved import NativeSolution  # noqa: PLC0415 - read native files on demand

        try:
            return NativeSolution(toml_io.load_path(path), native_format, exclude)
        except (OSError, ValueError, KeyError, IndexError, TypeError, RecursionError):
            return None
    lock = _read_prior_lock(path)
    return LockPreferences(lock, exclude=exclude) if lock is not None else None


def read_lockfile_packages(path: Path) -> dict[str, Version] | None:
    """Read unambiguous previous versions for a lockfile change summary."""
    lock = _read_prior_lock(path)
    return _summary_versions(lock) if lock is not None else None
