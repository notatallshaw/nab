"""Project previous PEP 751 versions onto current resolution contexts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nab_provider._vendor.packaging.pylock import (
    Pylock,
    PylockSelectError,
    PylockValidationError,
)
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider.environment import marker_environment
from nab_provider.tags import TagSet, parse_wheel_tag_set

from . import toml_io
from .paths import path_state

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

    from nab_provider._vendor.packaging.tags import Tag
    from nab_provider._vendor.packaging.version import Version
    from nab_provider.target import ResolveTarget

__all__ = ["LockPreferences", "read_lock_preferences"]


class LockPreferences:
    """Project a validated lock's versions into current resolution contexts."""

    __slots__ = ("_lock", "_tags", "known_packages")

    def __init__(self, lock: Pylock) -> None:
        """Collect the old lock's advertised tags once."""
        self._lock = lock
        self.known_packages = frozenset(
            str(package.name)
            for package in lock.packages
            if package.version is not None
        )
        advertised: set[Tag] = set()
        for package in lock.packages:
            for wheel in package.wheels or ():
                advertised.update(parse_wheel_tag_set(wheel.filename) or ())
        self._tags = tuple((tag, frozenset((tag,))) for tag in advertised)

    def summary_versions(self) -> dict[str, Version]:
        """Return recorded versions for the before/after display summary."""
        return {
            str(package.name): package.version
            for package in self._lock.packages
            if package.version is not None
        }

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
            selected = self._lock.select(
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


def read_lock_preferences(path: Path) -> LockPreferences | None:
    """Read optional prior version hints, refusing an unreadable or invalid lock."""
    if not path_state(path).should_read:
        return None
    try:
        with path.open("rb") as stream:
            lock = Pylock.from_dict(toml_io.load(stream))
    except (OSError, ValueError, RecursionError, PylockValidationError):
        return None
    return LockPreferences(lock)
