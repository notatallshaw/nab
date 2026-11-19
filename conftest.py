"""Hypothesis profiles and interpreter checks for workspace tests.

Three hypothesis profiles, selectable via the ``HYPOTHESIS_PROFILE`` env var:

- ``dev`` (default): low ``max_examples`` (20) for fast feedback.
- ``ci``: more thorough (200 examples), drawn fresh each run so the
  runs together search more of the space than any one could; a failure
  prints the ``@reproduce_failure`` blob that replays it. ``deadline=None``
  so a slow CI machine doesn't fail tests on the basis of wall time alone.
- ``deep``: 2000 examples; for nightly counter-example hunts.

Loading a profile only changes the default settings; tests that
explicitly construct a ``settings(...)`` decorator are unaffected.
The property suite under ``nab-*/tests/property*/`` uses explicit
``PROPERTY_SETTINGS``/``DEEP_SETTINGS``/``BRUTE_FORCE_SETTINGS``
decorators so its example budget is independent of the profile.
"""

from __future__ import annotations

import os
import sys
import sysconfig

import pytest
from hypothesis import HealthCheck, settings

pytest_plugins = ("nab_test_support.tests.conftest",)

settings.register_profile("dev", max_examples=20)
settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "deep",
    max_examples=2000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


def pytest_sessionfinish() -> None:
    """Reject free-threaded test runs that silently enabled the GIL."""
    if (
        sys.version_info >= (3, 13)
        and sysconfig.get_config_var("Py_GIL_DISABLED")
        and sys._is_gil_enabled()  # noqa: SLF001 - documented runtime GIL query
    ):
        pytest.exit("Free-threaded tests finished with the GIL enabled", returncode=1)
