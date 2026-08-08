"""Physical-host admission shared by the benchmark runners."""

from __future__ import annotations

import hashlib
import json
import signal
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nab_python.target import IMPLEMENTATION_MARKERS, PLATFORM_MARKERS, ResolveTarget

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


HOST_TAG_MISMATCH_REASON = (
    "marker environment requires wheel tags from a different host"
)
_HOST_MATCH_MARKERS = frozenset(
    key
    for candidates in (PLATFORM_MARKERS, IMPLEMENTATION_MARKERS)
    for candidate in candidates.values()
    for key in candidate
)


class BenchmarkTimeout(BaseException):
    """Escape resolver exception handlers when a benchmark exceeds its limit."""


def _marker_family_matches(
    marker_environment: Mapping[str, str],
    candidates: Mapping[str, Mapping[str, str]],
) -> bool:
    candidate_keys = {key for candidate in candidates.values() for key in candidate}
    declared = {
        key: value for key, value in marker_environment.items() if key in candidate_keys
    }
    return not declared or any(
        all(candidate.get(key) == value for key, value in declared.items())
        for candidate in candidates.values()
    )


def validate_target_marker_environment(
    scenario_name: str,
    marker_environment: Mapping[str, str],
) -> None:
    """Reject marker values that cannot describe a supported target."""
    if not _marker_family_matches(marker_environment, PLATFORM_MARKERS):
        msg = f"{scenario_name}: marker_environment describes no supported platform"
        raise ValueError(msg)
    if not _marker_family_matches(marker_environment, IMPLEMENTATION_MARKERS):
        msg = f"{scenario_name}: marker_environment describes no supported interpreter"
        raise ValueError(msg)


def parse_target_marker_environment(
    scenario_name: str,
    scenario: Mapping[str, object],
) -> dict[str, str]:
    """Read and validate one scenario's target marker overrides."""
    raw = scenario.get("marker_environment", {})
    if not isinstance(raw, dict) or any(
        type(key) is not str or type(value) is not str for key, value in raw.items()
    ):
        msg = f"{scenario_name}: marker_environment must be a table of strings"
        raise TypeError(msg)

    marker_environment = dict(raw)
    platform_system = scenario.get("platform_system")
    if platform_system is not None:
        if type(platform_system) is not str or not platform_system:
            msg = f"{scenario_name}: platform_system must be a non-empty string"
            raise TypeError(msg)
        declared_system = marker_environment.get("platform_system")
        if declared_system is not None and declared_system != platform_system:
            msg = f"{scenario_name}: platform_system conflicts with marker_environment"
            raise ValueError(msg)
        marker_environment["platform_system"] = platform_system

    validate_target_marker_environment(scenario_name, marker_environment)
    return marker_environment


def parse_requires_matching_host(
    scenario_name: str,
    scenario: Mapping[str, object],
    marker_environment: Mapping[str, str],
) -> bool:
    """Read whether a scenario requires physical-host wheel tags."""
    requires_matching_host = scenario.get("requires_matching_host", False)
    if type(requires_matching_host) is not bool:
        msg = f"{scenario_name}: requires_matching_host must be a boolean"
        raise TypeError(msg)
    if requires_matching_host and not _HOST_MATCH_MARKERS.intersection(
        marker_environment
    ):
        msg = (
            f"{scenario_name}: requires_matching_host needs a platform "
            "or interpreter marker"
        )
        raise ValueError(msg)
    return requires_matching_host


@dataclass(frozen=True, slots=True)
class TargetAdmission:
    """A resolve target, or why its required matching host is unavailable."""

    target: ResolveTarget | None
    inapplicable_reason: str | None


@dataclass(frozen=True, slots=True)
class BenchmarkHost:
    """One captured interpreter, marker environment, and wheel-tag set."""

    target: ResolveTarget
    python_runtime: str
    wall_timeout_seconds: int | None

    @classmethod
    def current(cls, wall_timeout_seconds: int) -> BenchmarkHost:
        """Capture the current host and its available timeout mechanism."""
        timeout_available = (
            getattr(signal, "SIGALRM", None) is not None
            and getattr(signal, "alarm", None) is not None
        )
        return cls(
            target=ResolveTarget.for_host(),
            python_runtime=sys.version,
            wall_timeout_seconds=wall_timeout_seconds if timeout_available else None,
        )

    def target_for(
        self,
        python_version: str,
        marker_environment: Mapping[str, str],
        *,
        requires_matching_host: bool,
    ) -> TargetAdmission:
        """Build a target, enforcing faithful tags when the host must match."""
        host_markers = self.target.marker_env
        host_tags = self.target.tags.ordered
        target = ResolveTarget.for_host_python(
            python_version,
            env_source=lambda: host_markers,
            tags_source=lambda: iter(host_tags),
        ).with_marker_overrides(marker_environment)

        if requires_matching_host and not target.tags_faithful:
            return TargetAdmission(None, HOST_TAG_MISMATCH_REASON)
        if any(
            target.marker_env.get(key) != value
            for key, value in marker_environment.items()
        ):
            msg = "resolve target did not retain the scenario marker environment"
            raise ValueError(msg)
        return TargetAdmission(target, None)

    def identity(self) -> dict[str, object]:
        """Return the host fields that determine benchmark execution."""
        tags = [str(tag) for tag in self.target.tags.ordered]
        return {
            "python": self.python_runtime,
            "marker_environment": dict(sorted(self.target.marker_env.items())),
            "wheel_tags_count": len(tags),
            "wheel_tags_hash": hashlib.sha256("\n".join(tags).encode()).hexdigest(),
        }

    @contextmanager
    def wall_timeout(self) -> Iterator[None]:
        """Enforce the captured wall-time limit when this platform supports it."""
        if self.wall_timeout_seconds is None:
            yield
            return

        sigalrm = signal.SIGALRM
        alarm = signal.alarm

        def handle_timeout(_signum: int, _frame: object) -> None:
            msg = f"scenario exceeded {self.wall_timeout_seconds}s wall-clock budget"
            raise BenchmarkTimeout(msg)

        previous_handler = signal.signal(sigalrm, handle_timeout)
        alarm(self.wall_timeout_seconds)
        try:
            yield
        finally:
            alarm(0)
            signal.signal(sigalrm, previous_handler)


def settings_hash(settings: Mapping[str, object]) -> str:
    """Hash the effective settings stored once in the run manifest."""
    encoded = json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
