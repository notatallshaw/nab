"""Typed marker environments retain their supplied values."""

import pytest

from nab_provider.environment import marker_environment
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget


def test_complete_mapping_is_copied_without_losing_fields() -> None:
    target = ResolveTarget.for_declared(
        python_version="3.12", spec=PlatformSpec("windows_amd64")
    )
    supplied = dict(target.marker_env)

    actual = marker_environment(supplied)

    assert actual == supplied
    actual["sys_platform"] = "linux"
    assert supplied["sys_platform"] == "win32"


def test_incomplete_mapping_is_not_filled_from_the_host() -> None:
    with pytest.raises(KeyError):
        marker_environment({"sys_platform": "win32"})
