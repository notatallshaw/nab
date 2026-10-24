"""The build policy a planned set of resolve targets permits."""

from __future__ import annotations

__lazy_modules__ = (
    "nab_resolver.diagnostics",
    "nab_provider.errors",
    "nab_provider.policy",
)

import logging
from typing import TYPE_CHECKING

from nab_provider.errors import ConfigError
from nab_provider.policy import BuildPolicy
from nab_resolver.diagnostics import DiagnosticMessage, Suggestion

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from nab_provider.overrides import IndexOverride, PackageOverride
    from nab_provider.target import ResolveTarget

__all__ = ["enforce_build_policy_for_targets"]

_logger = logging.getLogger(__name__)


def _has_declared_platform(targets: Sequence[ResolveTarget]) -> bool:
    """Whether any target declares a platform or implementation."""
    return any(target.platform_spec is not None for target in targets)


def enforce_build_policy_for_targets(
    *,
    targets: Sequence[ResolveTarget],
    build_policy: BuildPolicy,
    build_policy_set: bool,
    package_overrides: Sequence[PackageOverride],
    index_overrides: Mapping[str, IndexOverride],
    assume_universal_deps: bool = False,
) -> BuildPolicy:
    """Return the build policy the planned targets permit, or raise.

    A PEP 517 backend only ever runs on the host interpreter, so what a
    build reports is the host's metadata.  Two tiers follow:

    * A target declaring a ``platform`` or ``implementation`` defaults to
      ``never``. Explicit build policies require an ``assume-universal-deps``
      assertion; the provider checks its effective value for each candidate.
    * A python-axis-only retarget on the host machine warns and permits:
      the machine is still the host, so a build can run at all, and
      refusing every one of them would take the default case with it.  A
      deliberate deviation from pip.  Set ``build-policy = "never"`` to
      forbid it.

    The host target permits, so the default case builds freely.
    """
    if _has_declared_platform(targets):
        universal_assertion = (
            assume_universal_deps
            or any(rule.assume_universal_deps is True for rule in package_overrides)
            or any(
                rule.assume_universal_deps is True for rule in index_overrides.values()
            )
        )
        offending = _explicit_host_builds(
            build_policy_set=build_policy_set,
            build_policy=build_policy,
            package_overrides=package_overrides,
            index_overrides=index_overrides,
        )
        if offending and not universal_assertion:
            report = DiagnosticMessage(
                (
                    "a declared target requires assume-universal-deps = true "
                    f"to use host-built metadata; got {', '.join(offending)}. "
                    "A PEP 517 backend runs "
                    "on the host and reports the host's metadata, not the target's."
                ),
                suggestions=(
                    Suggestion(
                        "remove the setting (it defaults to 'never' for a declared "
                        "target) or set it to 'never'.  Remove the whole entry "
                        "where build-policy is its only key."
                    ),
                ),
            )
            raise ConfigError(report)

        if build_policy_set and universal_assertion:
            return build_policy
        return BuildPolicy.NEVER

    if not all(target.host_faithful for target in targets):
        _logger.warning(
            DiagnosticMessage(
                (
                    f"the resolve targets Python {targets[0].python_full_version} but "
                    "a build would run on the host interpreter and report its metadata"
                ),
                suggestions=(
                    Suggestion("set build-policy = 'never' to forbid builds"),
                ),
            )
        )

    return build_policy


def _explicit_host_builds(
    *,
    build_policy_set: bool,
    build_policy: BuildPolicy,
    package_overrides: Sequence[PackageOverride],
    index_overrides: Mapping[str, IndexOverride],
) -> list[str]:
    """Name every surface that explicitly asks for a non-``never`` build.

    An unset global is not offending: ``build-policy`` defaults to
    ``never`` for a target that forbids host builds rather than failing a
    project that never mentioned it.

    A per-package entry is named by the surface it was declared on, so a
    ``[[package-rules]]`` entry matching several requirements is named once.
    """
    offending: list[str] = []
    if build_policy_set and build_policy is not BuildPolicy.NEVER:
        offending.append(f"build-policy = {build_policy.value!r}")

    for pkg in package_overrides:
        bp = pkg.build_policy
        if bp is not None and bp is not BuildPolicy.NEVER:
            # Only an entry parsed from a config file carries a label.
            surface = pkg.source_label or str(pkg.requirement)
            entry = f"{surface} build-policy = {bp.value!r}"
            if entry not in offending:
                offending.append(entry)

    for name, index_override in index_overrides.items():
        bp = index_override.build_policy
        if bp is not None and bp is not BuildPolicy.NEVER:
            offending.append(f"index.{name} build-policy = {bp.value!r}")

    return offending
