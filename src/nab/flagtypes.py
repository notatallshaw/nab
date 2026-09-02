"""Literal choice types shared by command signatures and option rows."""

from __future__ import annotations

from typing import Literal

HttpBackend = Literal["urllib3", "httpx"]
LockFormat = Literal["pylock", "requirements", "requirements-without-hashes"]
ResolutionFlag = Literal["highest", "lowest", "lowest-direct"]
ModeFlag = Literal["specific", "universal"]
DistPolicyFlag = Literal[
    "wheel-only", "prefer-wheel", "wheel-or-sdist", "sdist-only", "sdist-install"
]
BuildPolicyFlag = Literal["never", "build-local", "build-remote"]
DecisionOrderFlag = Literal["arrival", "stable"]
MatrixOrderFlag = Literal["asc", "desc"]
ImplementationFlag = Literal["cpython", "pypy"]
