"""Keep direct index emission equivalent to native models and schema validation."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest
import tomli_w

from nab_project._lockfile import pylock
from nab_project._lockfile.coverage import CoverageError
from nab_project.lockfile import (
    ArchivePin,
    IndexPin,
    LocalPin,
    LockInput,
    LockValidationError,
    PinShape,
    Provenance,
    SdistArtifact,
    TargetLock,
    VcsPin,
    WheelArtifact,
)
from nab_provider._vendor.packaging.markers import Marker
from nab_provider._vendor.packaging.pylock import Package, PackageWheel, Pylock
from nab_provider._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget, environment_declaration
from nab_provider.value import replace

if TYPE_CHECKING:
    from pathlib import Path
    from types import FrameType


_HASHES = (("sha512", "b" * 128), ("sha256", "a" * 64))


def index_pin(*, size: int = 0) -> IndexPin:
    wheel = WheelArtifact(
        "demo-1.0-py3-none-any.whl",
        "https://example.com/demo-1.0-py3-none-any.whl",
        _HASHES,
        size=size,
    )
    sdist = SdistArtifact(
        "demo-1.0.tar.gz", "https://example.com/demo-1.0.tar.gz", _HASHES, size=0
    )
    return IndexPin(
        "demo",
        "1.0",
        "https://example.invalid/simple/",
        sdist=sdist,
        wheels=(wheel,),
        requires_python=">=3.10",
    )


def lock_input(pin: PinShape) -> LockInput:
    """Put a supported pin in an actual declared target and provenance context."""
    target = ResolveTarget.for_declared(
        python_version="3.12", spec=PlatformSpec("linux_x86_64")
    )
    return LockInput(
        targets={target.label: TargetLock(target=target, pins={pin.name: pin})},
        environments=[Marker(environment_declaration(target, []))],
        requires_python=">=3.10",
        extras=("my_extra", "my-extra"),
        dependency_groups=("test",),
        default_groups=("test",),
        provenance=Provenance(
            "0.0.0",
            datetime(2026, 10, 23, tzinfo=timezone.utc),
            ("nab", "lock"),
            "pyproject.toml",
            "specific",
        ),
    )


def observe_render(value: LockInput, *, lock_dir: Path) -> tuple[str, list[str]]:
    """Observe real policy, serialization and validator calls without replacing them."""
    codes = {
        pylock.validate_marker_disjointness.__code__: "disjointness",
        pylock.validate_marker_coverage.__code__: "coverage",
        pylock._index_document.__code__: "document",
        Pylock.to_dict.__code__: "native-document",
        Pylock.from_dict.__func__.__code__: "schema",
    }
    calls: list[str] = []

    def observed(frame: FrameType, event: str, _value: object) -> None:
        if event == "call" and (label := codes.get(frame.f_code)) is not None:
            calls.append(label)

    previous = sys.getprofile()
    sys.setprofile(observed)
    try:
        text = pylock.render_lock(value, lock_dir=lock_dir)
    finally:
        sys.setprofile(previous)
    return text, calls


@pytest.mark.parametrize("artifacts", ["remote", "local"])
def test_index_document_matches_native_values_types_order_and_text(
    tmp_path: Path, artifacts: str
) -> None:
    pin = index_pin()
    if artifacts == "local":
        assert pin.sdist is not None
        wheel = pin.wheels[0]
        sdist = pin.sdist
        pin = pin.replace(
            wheels=(
                WheelArtifact(
                    wheel.filename,
                    wheel.url,
                    wheel.hashes,
                    size=wheel.size,
                    local_path=tmp_path / "files" / wheel.filename,
                ),
            ),
            sdist=SdistArtifact(
                sdist.filename,
                sdist.url,
                sdist.hashes,
                size=sdist.size,
                local_path=tmp_path / "files" / sdist.filename,
            ),
        )
    value = lock_input(pin)
    expected = pylock.build_pylock(value, lock_dir=tmp_path).to_dict()
    fields, packages = pylock._prepare_pylock(
        value, lock_dir=tmp_path, package_output=pylock._pin_to_document
    )
    document = pylock._index_document(fields, packages)
    assert document == expected
    assert list(document) == list(expected)

    native_packages = expected["packages"]
    direct_packages = document["packages"]
    assert isinstance(direct_packages, list)
    for actual, native in zip(direct_packages, native_packages, strict=True):
        assert list(actual) == list(native)
        assert isinstance(actual["wheels"], tuple)
        assert list(actual["wheels"][0]) == list(native["wheels"][0])
        assert list(actual["wheels"][0]["hashes"]) == ["sha256", "sha512"]
        assert actual["wheels"][0]["size"] == 0
    assert isinstance(document["extras"], tuple)
    assert isinstance(document["dependency-groups"], tuple)
    assert isinstance(document["environments"], list)

    text, calls = observe_render(value, lock_dir=tmp_path)
    assert text == tomli_w.dumps(expected)
    assert calls == ["disjointness", "coverage", "document", "schema"]


def test_public_builder_still_returns_native_unvalidated_models(tmp_path: Path) -> None:
    value = lock_input(index_pin(size=False))
    model = pylock.build_pylock(value, lock_dir=tmp_path)
    assert isinstance(model, Pylock)
    assert isinstance(model.packages[0], Package)
    assert model.packages[0].wheels is not None
    wheel = model.packages[0].wheels[0]
    assert isinstance(wheel, PackageWheel)
    assert wheel.size is False

    with pytest.raises(LockValidationError, match="bool"):
        pylock.render_lock(value, lock_dir=tmp_path)


@pytest.mark.parametrize("source", ["local", "vcs", "archive"])
def test_other_source_kinds_keep_the_native_serializer(
    tmp_path: Path, source: str
) -> None:
    pin: PinShape
    if source == "local":
        pin = LocalPin("demo", "1.0", str(tmp_path / "project"))
    elif source == "vcs":
        pin = VcsPin(
            name="demo",
            version="1.0",
            repo_url="https://example.invalid/demo.git",
            bare_repo_url="https://example.invalid/demo.git",
            commit_id="a" * 40,
        )
    else:
        pin = ArchivePin(
            name="demo",
            version="1.0",
            url="https://example.invalid/demo-1.0.tar.gz",
            hashes=_HASHES,
        )
    value = lock_input(pin)
    expected = pylock.build_pylock(value, lock_dir=tmp_path).to_dict()
    text, calls = observe_render(value, lock_dir=tmp_path)
    assert text == tomli_w.dumps(expected)
    assert calls == ["disjointness", "coverage", "native-document", "schema"]


def test_mixed_sources_keep_index_artifacts_on_the_native_path(tmp_path: Path) -> None:
    value = lock_input(index_pin())
    label, lock = next(iter(value.targets.items()))
    value = replace(
        value,
        targets={
            label: replace(
                lock,
                pins={
                    "demo": index_pin(),
                    "local": LocalPin("local", "1.0", str(tmp_path / "project")),
                },
            )
        },
    )
    expected = pylock.build_pylock(value, lock_dir=tmp_path).to_dict()
    text, calls = observe_render(value, lock_dir=tmp_path)
    assert text == tomli_w.dumps(expected)
    assert calls == ["disjointness", "coverage", "native-document", "schema"]


def test_index_output_refuses_a_non_index_pin(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="Index document"):
        pylock._pin_to_document(
            LocalPin("demo", "1.0", str(tmp_path)), lock_dir=tmp_path
        )


def test_artifact_checks_precede_coverage_failure(tmp_path: Path) -> None:
    value = replace(
        lock_input(index_pin(size=-1)),
        environments=[Marker('python_version == "3.11"')],
    )
    with pytest.raises(LockValidationError, match="size"):
        pylock.render_lock(value, lock_dir=tmp_path)


def test_coverage_failure_precedes_root_specifier_parsing(tmp_path: Path) -> None:
    value = replace(
        lock_input(index_pin()),
        environments=[Marker('python_version == "3.11"')],
        requires_python="not a specifier",
    )
    with pytest.raises(CoverageError):
        pylock.render_lock(value, lock_dir=tmp_path)


def test_root_specifier_parsing_precedes_schema_validation(tmp_path: Path) -> None:
    value = replace(
        lock_input(index_pin(size=False)), requires_python="not a specifier"
    )
    with pytest.raises(InvalidSpecifier):
        pylock.render_lock(value, lock_dir=tmp_path)


def test_package_specifier_stringification_follows_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serialized: list[str] = []

    class ObservedSpecifier(SpecifierSet):
        def __str__(self) -> str:
            serialized.append("specifier")
            return super().__str__()

    monkeypatch.setattr(pylock, "SpecifierSet", ObservedSpecifier)
    value = replace(
        lock_input(index_pin()),
        environments=[Marker('python_version == "3.11"')],
        requires_python=None,
    )
    with pytest.raises(CoverageError):
        pylock.render_lock(value, lock_dir=tmp_path)
    assert serialized == []

    text, calls = observe_render(
        replace(value, environments=lock_input(index_pin()).environments),
        lock_dir=tmp_path,
    )
    assert text
    assert serialized == ["specifier"]
    assert calls == ["disjointness", "coverage", "document", "schema"]


def test_document_collections_do_not_alias_other_emissions(tmp_path: Path) -> None:
    value = lock_input(index_pin())
    root, packages = pylock._prepare_pylock(
        value, lock_dir=tmp_path, package_output=pylock._pin_to_document
    )
    document = pylock._index_document(root, packages)
    emitted = document["packages"]
    assert isinstance(emitted, list)
    emitted[0]["wheels"][0]["hashes"]["sha256"] = "c" * 64
    tool = document["tool"]
    assert isinstance(tool, dict)
    tool["nab"]["command-line"].append("changed")

    fresh = pylock.render_lock(value, lock_dir=tmp_path)
    assert "c" * 64 not in fresh
    assert "changed" not in fresh
    assert value.provenance is not None
    assert value.provenance.command_line == ("nab", "lock")
    assert value.targets[next(iter(value.targets))].pins["demo"] == index_pin()
