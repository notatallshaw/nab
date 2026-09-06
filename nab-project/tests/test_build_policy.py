"""Build policy for host and declared-platform targets.

A declared platform or implementation forbids host builds. Changing only
Python warns instead.
"""

from __future__ import annotations

import pytest

from nab_project.build_policy import enforce_build_policy_for_targets
from nab_provider.errors import ConfigError
from nab_provider.overrides import IndexOverride
from nab_provider.policy import BuildPolicy
from nab_provider.tags import PlatformSpec
from nab_provider.target import Matrix, ResolveTarget
from nab_provider.testing import pkg_override

DECLARED = Matrix(python="==3.12", platforms=(PlatformSpec("linux_x86_64"),)).expand()


def test_the_host_target_keeps_the_declared_policy() -> None:
    assert (
        enforce_build_policy_for_targets(
            targets=[ResolveTarget.for_host()],
            build_policy=BuildPolicy.BUILD_LOCAL,
            build_policy_set=True,
            package_overrides=(),
            index_overrides={},
        )
        is BuildPolicy.BUILD_LOCAL
    )


def test_a_host_python_retarget_warns_and_permits(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The machine is still the host, so a build can run and only warns."""
    permitted = enforce_build_policy_for_targets(
        targets=[ResolveTarget.for_host_python("3.11")],
        build_policy=BuildPolicy.BUILD_LOCAL,
        build_policy_set=True,
        package_overrides=(),
        index_overrides={},
    )

    assert permitted is BuildPolicy.BUILD_LOCAL
    assert "a build would run on the host interpreter" in caplog.text


def test_a_declared_target_forces_never_when_nothing_asks_to_build() -> None:
    """An unset global defaults to ``never`` rather than failing the project."""
    assert (
        enforce_build_policy_for_targets(
            targets=DECLARED,
            build_policy=BuildPolicy.BUILD_LOCAL,
            build_policy_set=False,
            package_overrides=(pkg_override("demo", build_policy=BuildPolicy.NEVER),),
            index_overrides={"private": IndexOverride()},
        )
        is BuildPolicy.NEVER
    )


def test_an_explicit_global_policy_raises_under_a_declared_target() -> None:
    with pytest.raises(ConfigError, match="got build-policy = 'build-local'"):
        enforce_build_policy_for_targets(
            targets=DECLARED,
            build_policy=BuildPolicy.BUILD_LOCAL,
            build_policy_set=True,
            package_overrides=(),
            index_overrides={},
        )


def test_the_message_names_every_offending_override() -> None:
    """A project reading the error has to know which entry to edit."""
    with pytest.raises(ConfigError) as raised:
        enforce_build_policy_for_targets(
            targets=DECLARED,
            build_policy=BuildPolicy.NEVER,
            build_policy_set=False,
            package_overrides=(
                pkg_override(
                    "demo",
                    build_policy=BuildPolicy.BUILD_REMOTE,
                    source_label="pyproject.toml: packages.'demo'",
                ),
            ),
            index_overrides={
                "private": IndexOverride(build_policy=BuildPolicy.BUILD_LOCAL)
            },
        )

    message = str(raised.value)

    assert "pyproject.toml: packages.'demo' build-policy = 'build-remote'" in message
    assert "index.private build-policy = 'build-local'" in message


def test_one_rule_entry_is_named_once_however_many_it_matches() -> None:
    """A ``match`` list is one declaration, so it is one entry to edit."""
    with pytest.raises(ConfigError) as raised:
        enforce_build_policy_for_targets(
            targets=DECLARED,
            build_policy=BuildPolicy.NEVER,
            build_policy_set=False,
            package_overrides=tuple(
                pkg_override(
                    name,
                    build_policy=BuildPolicy.BUILD_LOCAL,
                    source_label="pyproject.toml: package-rules[0]",
                )
                for name in ("foo", "bar")
            ),
            index_overrides={},
        )

    message = str(raised.value)

    assert "pyproject.toml: package-rules[0] build-policy = 'build-local'" in message
    assert message.count("package-rules[0]") == 1


def test_an_override_with_no_declared_surface_names_its_requirement() -> None:
    """With no source label, the refusal names the override's requirement."""
    with pytest.raises(ConfigError, match=r"got demo>=2 build-policy = 'build-local'"):
        enforce_build_policy_for_targets(
            targets=DECLARED,
            build_policy=BuildPolicy.NEVER,
            build_policy_set=False,
            package_overrides=(
                pkg_override("demo>=2", build_policy=BuildPolicy.BUILD_LOCAL),
            ),
            index_overrides={},
        )
