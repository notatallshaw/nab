"""The build policy a planned set of resolve targets permits."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nab_provider.errors import ConfigError
from nab_provider.policy import BuildPolicy

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from nab_provider.overrides import IndexOverride, PackageOverride
    from nab_provider.target import ResolveTarget

__all__ = ["enforce_build_policy_for_targets"]

_logger = logging.getLogger(__name__)


def _forbids_host_builds(targets: Sequence[ResolveTarget]) -> bool:
    """Whether any target impersonates a machine other than the host's.

    A declared target (a matrix tuple, or an environment naming a platform
    or implementation) carries a :class:`PlatformSpec`; the host and a
    host-python retarget do not.
    """
    return any(target.platform_spec is not None for target in targets)


def enforce_build_policy_for_targets(
    *,
    targets: Sequence[ResolveTarget],
    build_policy: BuildPolicy,
    build_policy_set: bool,
    package_overrides: Sequence[PackageOverride],
    index_overrides: Mapping[str, IndexOverride],
) -> BuildPolicy:
    """Return the build policy the planned targets permit, or raise.

    A PEP 517 backend only ever runs on the host interpreter, so what a
    build reports is the host's metadata.  Two tiers follow:

    * A target that moves the platform axis (a matrix, or an environment
      naming a ``platform`` or ``implementation``) forbids host builds:
      ``build-policy`` is forced to ``never`` and an explicit non-``never``
      value, global or in any override, is an error.  This matches pip,
      which requires ``--only-binary=:all:`` under ``--platform``.
    * A python-axis-only retarget on the host machine warns and permits:
      the machine is still the host, so a build can run at all, and
      refusing every one of them would take the default case with it.  A
      deliberate deviation from pip.  Set ``build-policy = "never"`` to
      forbid it.

    The host target permits, so the default case builds freely.
    """
    if _forbids_host_builds(targets):
        offending = _explicit_host_builds(
            build_policy_set=build_policy_set,
            build_policy=build_policy,
            package_overrides=package_overrides,
            index_overrides=index_overrides,
        )
        if offending:
            msg = (
                "a declared target cannot build on the host, so build-policy"
                f" must be 'never'; got {', '.join(offending)}.  A PEP 517"
                " backend runs on the host and reports the host's metadata,"
                " not the target's.  Remove the setting (it defaults to"
                " 'never' for a declared target) or set it to 'never'."
            )
            raise ConfigError(msg)
        return BuildPolicy.NEVER
    if not all(target.host_faithful for target in targets):
        _logger.warning(
            "the resolve targets Python %s but a build would run on the host"
            " interpreter and report its metadata; set build-policy = 'never'"
            " to forbid builds",
            targets[0].python_full_version,
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
    """
    offending: list[str] = []
    if build_policy_set and build_policy is not BuildPolicy.NEVER:
        offending.append(f"build-policy = {build_policy.value!r}")
    for pkg in package_overrides:
        bp = pkg.build_policy
        if bp is not None and bp is not BuildPolicy.NEVER:
            offending.append(f"packages.{pkg.requirement} build-policy = {bp.value!r}")
    for name, index_override in index_overrides.items():
        bp = index_override.build_policy
        if bp is not None and bp is not BuildPolicy.NEVER:
            offending.append(f"index.{name} build-policy = {bp.value!r}")
    return offending
