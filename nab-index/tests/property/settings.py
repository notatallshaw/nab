"""Example budgets for the index property suites."""

import os

from hypothesis import HealthCheck, settings

_DEEP = os.environ.get("HYPOTHESIS_PROFILE") == "deep"

PROPERTY_SETTINGS = settings(
    max_examples=1000 if _DEEP else 100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
DEEP_SETTINGS = settings(PROPERTY_SETTINGS, max_examples=3000 if _DEEP else 300)
