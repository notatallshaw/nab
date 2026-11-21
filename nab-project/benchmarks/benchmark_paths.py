"""Portable run labels and filename components for benchmark reports."""

import argparse

_PORTABLE_COMPONENT_START = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
)
_PORTABLE_COMPONENT_CHARS = _PORTABLE_COMPONENT_START | frozenset(".-")
_MAX_PORTABLE_COMPONENT_LENGTH = 128
_WINDOWS_RESERVED_COMPONENTS = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)


def is_portable_path_component(
    value: str,
    *,
    max_length: int = _MAX_PORTABLE_COMPONENT_LENGTH,
) -> bool:
    """Return whether *value* is a portable single filename component."""
    if (
        not value
        or len(value) > max_length
        or value[0] not in _PORTABLE_COMPONENT_START
        or value.endswith(".")
        or any(char not in _PORTABLE_COMPONENT_CHARS for char in value)
    ):
        return False
    windows_basename = value.partition(".")[0].casefold()
    return windows_basename not in _WINDOWS_RESERVED_COMPONENTS


def result_directory_label(value: str) -> str:
    """Validate a user-provided results-directory label."""
    if not is_portable_path_component(value):
        msg = f"result label must use a portable ASCII filename, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return value
