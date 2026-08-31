"""The token types the four command signatures annotate a choice flag with.

Each alias is the choice set of the row that feeds it, so the parameter a
command declares refuses a token the flag would not have accepted.  They
stay hand-written because a checker reads a ``Literal`` only where its
members are written out, and ``mirrors=`` on the row that carries one holds
it to the enum the tokens came from.

They sit here rather than in ``nab.cli`` because :mod:`nab.optiontable`
reads them, and a declaration that imported the CLI would pull tyro and the
four command modules in with it.  Nothing is won back on the command path
for now: the command modules import ``nab.cli`` anyway, for ``app`` and the
two annotated types, so this is one module more than the branch before it.
"""

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
