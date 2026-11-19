"""Check the Simple API version advertised by a project page."""

from __future__ import annotations

import logging
import re

from nab_provider.errors import IndexAccessError

from .urls import strip_userinfo

_LOGGER = logging.getLogger(__name__)
_MAJOR_MINOR = re.compile(r"([0-9]+)\.([0-9]+)")


def check_repository_version(value: object, page: str) -> None:
    """Accept unversioned and compatible pages, and report incompatible versions."""
    if value is None:
        return

    match = _MAJOR_MINOR.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        msg = (
            f"{strip_userinfo(page)} declares invalid Simple API version {value!r}; "
            "expected major.minor"
        )
        raise IndexAccessError(msg)

    major = match[1].lstrip("0") or "0"
    minor = match[2].lstrip("0") or "0"
    if major not in {"0", "1"}:
        msg = (
            f"{strip_userinfo(page)} requires Simple API version {value}; "
            "Nab supports major version 1"
        )
        raise IndexAccessError(msg)
    if major == "1" and minor not in {"0", "1", "2", "3", "4"}:
        _LOGGER.warning(
            "%s declares Simple API version %s; Nab understands versions through 1.4",
            strip_userinfo(page),
            value,
        )
