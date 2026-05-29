"""Progress signals emitted during resolution."""

from __future__ import annotations

from typing import Protocol

from nab_resolver.resolver import ResolverObserver

from ._vendor.packaging.version import Version
from .provider import split_extra


class ProgressReporter(Protocol):
    """Receives resolution progress events as they happen."""

    def listing_fetched(self, package: str) -> None:
        """Note that ``package``'s index listing finished fetching."""
        ...

    def package_pinned(self, package: str) -> None:
        """Note that the resolver decided a version for ``package``."""
        ...


class PinObserver(ResolverObserver[str, Version]):
    """Forward base-package pins from the solver to a progress reporter.

    Extras-proxy decisions (``name[extra]``) share their base package's
    version, so only base-package pins are reported, matching the pins
    that reach the lockfile.
    """

    def __init__(self, progress: ProgressReporter) -> None:
        """Record the ``progress`` reporter to forward pins to."""
        self._progress = progress

    def on_decision(
        self,
        package: str,
        version: Version,  # noqa: ARG002
        level: int,  # noqa: ARG002
    ) -> None:
        """Report ``package`` as pinned unless it is an extras proxy."""
        if split_extra(package)[1] is None:
            self._progress.package_pinned(package)
