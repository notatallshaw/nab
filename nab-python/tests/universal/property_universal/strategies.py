"""Shared Hypothesis strategies for nab-python universal property tests."""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

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

LINUX_ENV: dict[str, str] = {
    "python_version": "3.11",
    "python_full_version": "3.11.0",
    "implementation_name": "cpython",
    "implementation_version": "3.11.0",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "sys_platform": "linux",
}
