"""Render a refused command line for stderr without writing it.

The runtime ``UsageError`` import avoids a ``TYPE_CHECKING`` block, which would
import :mod:`typing` on this error-only path.
"""

from __future__ import annotations

import difflib

from nab._cli.parse import UsageError  # noqa: TC001 - see the module docstring

__all__ = ["diagnose", "suggest"]

# difflib's own defaults, which the suggestion pass then narrows.
_MATCHES = 3
_CUTOFF = 0.6

# What a message offers, at most.  At three, --all-xtras answers with
# --all-extras, --extras and --all-groups, and the third is noise.
_SUGGESTIONS = 2

# Match ``nab.output`` without importing the output layer on this error-only path.
_RED = "\033[31m"
_CYAN = "\033[36m"
_RESET = "\033[0m"


def _paint(text: str, code: str, *, color: bool) -> str:
    """Wrap ``text`` in ``code``, or hand it back plain when colour is off."""
    return f"{code}{text}{_RESET}" if color else text


def suggest(token: str, candidates: tuple[str, ...]) -> tuple[str, ...]:
    """Offer the spellings ``token`` might have meant, best first, at most two.

    Matching runs on the names with their dashes stripped, because a
    leading ``--`` adds two matching characters to every ratio and pushes
    unrelated names over difflib's cutoff.  An underscored typo is
    normalized the way the walk normalizes it, so it reaches its dashed
    row.  Prefix matches come first, in declaration order, because
    difflib alone misses a long name behind a short typo.  A token that is
    nothing but dashes prefixes every name, so it is offered nothing.
    """
    typo = token.lstrip("-").replace("_", "-")
    if not typo:
        return ()

    spellings: dict[str, str] = {}
    for candidate in candidates:
        spellings.setdefault(candidate.lstrip("-"), candidate)

    names = list(spellings)
    prefixed = [name for name in names if name.startswith(typo)]
    close = difflib.get_close_matches(typo, names, n=_MATCHES, cutoff=_CUTOFF)
    picked = prefixed + [name for name in close if name not in prefixed]

    return tuple(spellings[name] for name in picked[:_SUGGESTIONS])


def diagnose(error: UsageError, *, color: bool = False) -> str:
    """Format an error, suggestions, and help hint for the caller to write.

    The result has at most three lines and no option list. ``color`` is
    the caller's decision for the output stream.
    """
    opener = _paint(f"{error.prog}:", _RED, color=color)
    lines = [f"{opener} {error.message}"]

    named = suggest(error.token, error.candidates) if error.token else ()
    if named:
        lines.append(_suggestion(named, color=color))

    lines.append(f"Try '{error.prog} --help' for more information.")

    return "\n".join(lines) + "\n"


def _suggestion(named: tuple[str, ...], *, color: bool) -> str:
    """Word the did-you-mean line, singular or plural.

    The quotes stay plain, so the spellings are still marked off when the
    colour is stripped.
    """
    quoted = ", ".join(f"'{_paint(name, _CYAN, color=color)}'" for name in named)
    if len(named) == 1:
        return f"did you mean {quoted}?"
    return f"did you mean one of {quoted}?"
