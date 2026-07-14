"""Shared Hypothesis strategies for nab-python universal property tests."""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

from nab_python.tags import PlatformSpec
from nab_python.target import ResolveTarget

_DEEP = os.environ.get("HYPOTHESIS_PROFILE") == "deep"

PROPERTY_SETTINGS = settings(
    max_examples=2000 if _DEEP else 200,
    deadline=None if _DEEP else 2000,
    suppress_health_check=[HealthCheck.too_slow],
)
"""Default settings for fast properties.

The ``deep`` profile (env ``HYPOTHESIS_PROFILE=deep``) bumps
``max_examples`` to 2000 for nightly counter-example hunts.
"""

DEEP_SETTINGS = settings(
    max_examples=5000 if _DEEP else 500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
"""Heavier settings for properties that benefit from more examples."""

LINUX_TARGET = ResolveTarget.for_declared(
    python_version="3.11", spec=PlatformSpec("linux_x86_64")
)
"""The CPython 3.11 linux_x86_64 target the property fixtures resolve against."""
