"""Top-level conftest: hypothesis profile registration.

Three profiles, selectable via the ``HYPOTHESIS_PROFILE`` env var:

- ``dev`` (default): low ``max_examples`` (20) for fast feedback.
- ``ci``: more thorough (200 examples) plus ``derandomize=True`` so
  failures are reproducible from the seed printed in the assertion;
  ``deadline=None`` so a slow CI machine doesn't fail tests on
  the basis of wall time alone.
- ``deep``: 2000 examples; for nightly counter-example hunts.

Loading a profile only changes the default settings; tests that
explicitly construct a ``settings(...)`` decorator are unaffected.
The property suite under ``nab-*/tests/property*/`` uses explicit
``PROPERTY_SETTINGS``/``DEEP_SETTINGS``/``BRUTE_FORCE_SETTINGS``
decorators so its example budget is independent of the profile.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

settings.register_profile("dev", max_examples=20)
settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "deep",
    max_examples=2000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
