"""Concurrency limits for project fetches and downloads."""


def validate_max_concurrency(limit: int) -> None:
    """Require a permit for at least one in-flight request."""
    if limit < 1:
        msg = "max_concurrency must be at least 1"
        raise ValueError(msg)
