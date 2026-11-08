"""Read namespaced CLI assignments using the configuration value grammar."""

from __future__ import annotations

__lazy_modules__ = (
    "nab._cli.definition.model",
    "nab.config.values",
    "nab_index.urls",
    "nab_project.toml_writer",
    "nab_provider.pep508",
)

from typing import TYPE_CHECKING

from nab_index.urls import strip_userinfo
from nab_project.toml_writer import dumps_value
from nab_provider.pep508 import parse_requirement

from .values import SourceConfigError, parse_toml_value

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .._cli.definition.model import Opt


class CliSetting:
    """A whole configuration value and the argument that supplied it."""

    __slots__ = ("key", "value", "written")

    def __init__(self, key: str, value: object, written: str) -> None:
        """Keep the parsed value beside its original CLI argument."""
        self.key = key
        self.value = value
        self.written = written

    @property
    def display(self) -> str:
        """Render a replayable override without embedding URL credentials."""
        if self.key not in {
            "indexes",
            "vcs",
            "vcs-sources",
            "archive-sources",
            "packages",
            "package-rules",
            "constraints",
        }:
            return self.written

        try:
            safe = _safe_value(self.value, self.key)
        except ValueError as exc:
            msg = f"--setting {self.key}: invalid URL or requirement in the override"
            raise SourceConfigError(msg) from exc

        if safe is self.value or safe == self.value:
            return self.written

        name = self.written.partition("=")[0]
        return name + "=" + dumps_value(safe)


def _safe_value(value: object, field: str) -> object:
    """Strip credentials from URL fields while retaining unrelated literal text."""
    if isinstance(value, str):
        if field in {"url", "allowed-repos"}:
            return strip_userinfo(value)

        # Requirement URLs need PEP 508 parsing; other strings stay unchanged.
        if field in {"dependencies", "constraints"}:
            requirement = parse_requirement(value)
            if requirement.url is not None:
                return value.replace(requirement.url, strip_userinfo(requirement.url))
        return value

    if isinstance(value, list):
        return [_safe_value(item, field) for item in value]

    if isinstance(value, dict):
        return {key: _safe_value(item, key) for key, item in value.items()}

    return value


def parse_settings(
    arguments: Sequence[str],
    specs: Mapping[str, Opt],
    native: Mapping[str, object],
) -> dict[str, CliSetting]:
    """Read scope.key=TOML assignments, refusing competing flag forms."""
    result: dict[str, CliSetting] = {}
    for argument in arguments:
        name, separator, expression = argument.partition("=")
        scope, dot, key = name.strip().partition(".")
        if not separator or not dot or scope not in {"project", "user"}:
            msg = "--setting expects project.KEY=TOML or user.KEY=TOML"
            raise SourceConfigError(msg)

        spec = specs.get(key)
        if spec is None:
            msg = f"--setting: unknown configuration key {key!r}"
            raise SourceConfigError(msg)

        if scope != spec.scope_name:
            msg = f"--setting: {key!r} belongs to the {spec.scope_name} namespace"
            raise SourceConfigError(msg)

        # Mixed flag forms are refused before the value is parsed.
        if key in native:
            msg = f"--setting: {key!r} is also set by a dedicated flag; use one form"
            raise SourceConfigError(msg)

        value = parse_toml_value(expression, f"--setting {name}")
        result[key] = CliSetting(key, value, argument)

    return result
