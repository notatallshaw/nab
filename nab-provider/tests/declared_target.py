"""Declared CPython targets used by provider tests."""

from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget


def target(python: str = "3.12", platform: str = "linux_x86_64") -> ResolveTarget:
    """Build a real declared target for shared-versus-fresh comparisons."""
    return ResolveTarget.for_declared(
        python_version=python, spec=PlatformSpec(platform)
    )
