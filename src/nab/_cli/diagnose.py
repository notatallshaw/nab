"""Turn a refused command line into the text that goes to stderr.

Loaded only from the branch that already has a :class:`UsageError`, so
:mod:`difflib` reaches the process only on a line that was refused.  The
text is returned rather than written, so the caller keeps the one write
site per stream.

:class:`UsageError` is imported at runtime rather than behind a
``TYPE_CHECKING`` block, because that block is itself an ``import
typing`` and this module is on the path of every mistyped command.
"""

from __future__ import annotations

import difflib

from nab._cli.parse import UsageError  # noqa: TC001 - see the module docstring

__all__ = ["diagnose", "suggest"]

# difflib's own defaults, which the suggestion pass then narrows.
_MATCHES = 3
_CUTOFF = 0.6

# What a message offers, at most.  At three, a half-typed --cache answers
# with --cache-dir, --cache and --no-cache, and the third is noise.
_SUGGESTIONS = 2

# Red opens a refusal, as it does for :meth:`nab.output.Printer.error`, and
# cyan marks a spelling the reader can type.  Neither is combined with bold,
# because bold plus a colour selects the bright slot, which a theme may map
# to a grey with less contrast than the plain hue.  The codes are written
# here rather than taken from :mod:`nab.output`, whose import would put the
# whole output layer on the path of every mistyped command.
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
    """Write out ``error``: what is wrong, and then what to try.

    Three lines at most, and no option list: a page of spellings is what
    the user asks for with ``--help``.  ``color`` is the caller's decision,
    already made against the stream the text is written to.
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
