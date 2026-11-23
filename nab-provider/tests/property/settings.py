"""Sample budgets for the generated package-policy cases."""

import os

from hypothesis import HealthCheck, settings

PROPERTY_SETTINGS = settings(
    max_examples=1000 if os.environ.get("HYPOTHESIS_PROFILE") == "deep" else 100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
