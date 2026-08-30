"""Render the effective configuration for ``nab config``.

``list`` prints every registry row with the value that won and where it came
from, ``get`` prints one row's value alone, and ``explain`` prints one row's
whole stack with the winner marked and every source it beat below it.  All
three read the map :func:`nab.config.layers.resolve_config` returns, so what
is printed is what the run would use.

Nothing here reads a source or decides a value; a rejected source reaches
these renderers only as the ``rejected`` records the ladder collected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .hooks import SourceConfigError
from .registry import BY_KEY, OPTIONS, Scope, SourceKind

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from .registry import EffectiveValue, RejectedLayer

__all__ = [
    "orphan_rejections",
    "project_cli_override_notice",
    "project_cli_override_records",
    "render_explain",
    "render_get",
    "render_list",
]


def _ordered(effective: Mapping[str, EffectiveValue]) -> list[EffectiveValue]:
    return [effective[spec.key] for spec in OPTIONS]


# Column widths for the ``nab config list`` table, shared by the header
# and every row so the two cannot drift.  The trailing ``origin`` column
# is unpadded (last field).
_LIST_KEY_W = 20
_LIST_VALUE_W = 20
_LIST_SCOPE_W = 9
# Status column width for ``nab config explain`` (winner/shadowed/rejected).
_EXPLAIN_STATUS_W = 9


def orphan_rejections(
    rejected: Iterable[RejectedLayer],
) -> tuple[RejectedLayer, ...]:
    """Rejections that name no registry option, so attach to no key.

    An unknown standalone ``nab.toml`` key or an unknown ``NAB_*`` var is
    recorded with ``key`` set to the offending name, which matches no
    registry key, so :func:`resolve_config` attaches it to no
    :class:`EffectiveValue` and no ``explain <key>`` reaches it.  These
    orphans are surfaced by :func:`render_list` instead.
    """
    return tuple(rej for rej in rejected if rej.key not in BY_KEY)


def render_list(
    effective: Mapping[str, EffectiveValue],
    *,
    rejected: Iterable[RejectedLayer] = (),
) -> str:
    """Render every effective option: value, scope, origin.

    ``rejected`` (when collecting for ``--include-rejected``) adds a
    trailing section listing every rejected source: a key set outside its
    scope, and an unknown key or ``NAB_*`` var.  ``explain`` reaches the
    former (it attaches to the named option) but not the latter (it names
    no option), so ``list`` is the one place that shows both together.
    """
    header = (
        f"{'key':<{_LIST_KEY_W}} {'value':<{_LIST_VALUE_W}}"
        f" {'scope':<{_LIST_SCOPE_W}} origin"
    )
    lines = [header]
    for ev in _ordered(effective):
        rendered = ev.spec.render(ev.value)
        lines.append(
            f"{ev.spec.key:<{_LIST_KEY_W}} {rendered:<{_LIST_VALUE_W}}"
            f" {ev.origin.scope:<{_LIST_SCOPE_W}} {ev.origin.label}"
        )
    rejected = tuple(rejected)
    if rejected:
        lines.append("")
        lines.append("rejected:")
        lines.extend(
            f"  {rej.origin.scope:<{_LIST_SCOPE_W}} {rej.origin.label}"
            f"  {rej.key}: {rej.reason}"
            for rej in rejected
        )
    return "\n".join(lines) + "\n"


def render_get(effective: Mapping[str, EffectiveValue], key: str) -> str:
    """Render only the effective value of ``key``."""
    ev = _require_key(effective, key)
    return ev.spec.render(ev.value) + "\n"


def render_explain(
    effective: Mapping[str, EffectiveValue],
    key: str,
    *,
    include_rejected: bool = False,
) -> str:
    """Render the full shadowed stack for ``key``.

    The highest source is the ``winner`` and carries a ``>`` gutter;
    every source it beats is ``shadowed``.  With ``include_rejected`` the
    category-rejected sources (a source that tried to set ``key`` but was
    not allowed) are listed too, labelled ``rejected``.
    """
    ev = _require_key(effective, key)
    lines = [f"{key} ({ev.spec.scope.value}, {ev.spec.type_label})"]
    winner_index = len(ev.stack) - 1
    for i, (origin, value) in enumerate(ev.stack):
        gutter = ">" if i == winner_index else " "
        status = "winner" if i == winner_index else "shadowed"
        rendered = ev.spec.render(value)
        lines.append(
            f"{gutter} {origin.scope:<{_LIST_SCOPE_W}} {rendered:<{_LIST_VALUE_W}}"
            f" {status:<{_EXPLAIN_STATUS_W}} {origin.label}"
        )
    if include_rejected:
        lines.extend(
            f"  {rej.origin.scope:<{_LIST_SCOPE_W}} {'-':<{_LIST_VALUE_W}}"
            f" {'rejected':<{_EXPLAIN_STATUS_W}} {rej.origin.label} ({rej.reason})"
            for rej in ev.rejected
        )
    return "\n".join(lines) + "\n"


def _require_key(effective: Mapping[str, EffectiveValue], key: str) -> EffectiveValue:
    ev = effective.get(key)
    if ev is None:
        valid = sorted(BY_KEY)
        msg = f"unknown config key {key!r}; known keys are {valid!r}"
        raise SourceConfigError(msg)
    return ev


def project_cli_override_records(
    effective: Mapping[str, EffectiveValue],
) -> tuple[tuple[str, str], ...]:
    """Return the ``(flag, value)`` pairs for PROJECT options set on the CLI.

    A PROJECT option changes the resolved set, so a CLI override means the
    result no longer derives from the committed files alone.  These pairs
    drive both the reproducibility notice and the auditable record written
    into the lockfile provenance.  A file-only row (``cli_flag`` is ``None``)
    is never CLI-settable, so it cannot appear.
    """
    records: list[tuple[str, str]] = []
    for spec in OPTIONS:
        if spec.scope is not Scope.PROJECT:
            continue
        ev = effective[spec.key]
        if ev.origin.kind is not SourceKind.CLI or spec.cli_flag is None:
            continue
        records.append((spec.cli_flag, spec.render(ev.value)))
    return tuple(records)


def project_cli_override_notice(
    effective: Mapping[str, EffectiveValue],
    *,
    produces_lock: bool = True,
) -> str | None:
    """Reproducibility notice for any PROJECT option set on the CLI.

    Returns a notice listing every PROJECT override that came from the CLI
    rung; ``None`` when no PROJECT option was set on the CLI.

    ``produces_lock`` tailors the wording: ``nab lock`` produces a lock, so
    the notice warns the lock will not derive from the committed files; the
    read-only ``nab config`` inspector produces no lock, so it warns only
    that the displayed values reflect a CLI override.
    """
    records = project_cli_override_records(effective)
    if not records:
        return None
    if produces_lock:
        header = (
            "notice: project-scope overrides were applied from the CLI; the lock"
            " they produce does not derive from the committed pyproject/nab.toml"
            " alone:"
        )
    else:
        header = (
            "notice: project-scope overrides were applied from the CLI; the"
            " values below reflect that override, not the committed"
            " pyproject/nab.toml alone:"
        )
    lines = [header]
    lines.extend(f"  {flag} -> {rendered}" for flag, rendered in records)
    return "\n".join(lines) + "\n"
