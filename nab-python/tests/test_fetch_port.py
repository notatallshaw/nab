"""Tests for the fetch port: what it declares, and who satisfies it."""

from __future__ import annotations

import ast
import inspect
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_python.fetch import FetchCoordinator
from nab_python.fetch_port import FetchPort, Waitable

SRC = Path(__file__).resolve().parents[1] / "src" / "nab_python"

PROVIDER_SOURCES = (SRC / "provider.py", *sorted((SRC / "_provider").glob("*.py")))
"""The modules the census reads.

A module outside this list can read the handle without the census seeing it, so
the list is part of what the census asserts rather than a derived fact. Inside
it the census is exact: it fails on any occurrence of the handle it cannot
follow, so a read cannot hide behind an alias."""


def declared_members(protocol: type) -> frozenset[str]:
    """The names a protocol declares, ignoring what Protocol itself adds."""
    return frozenset(name for name in vars(protocol) if not name.startswith("_"))


def parameter_shape(func: object) -> list[tuple[str, inspect._ParameterKind, object]]:
    """The name, kind and default of each parameter, in declaration order."""
    return [
        (param.name, param.kind, param.default)
        for param in inspect.signature(func).parameters.values()  # type: ignore[arg-type]
    ]


def is_the_handle(node: ast.expr) -> bool:
    """Whether ``node`` evaluates to the coordinator handle.

    Two spellings reach it: the constructor's parameter, and the ``.coordinator``
    attribute the provider and its helpers read it back from.
    """
    if isinstance(node, ast.Name):
        return node.id == "coordinator"
    return isinstance(node, ast.Attribute) and node.attr == "coordinator"


def binding_of(node: ast.AST) -> tuple[ast.expr, Sequence[ast.expr]] | None:
    """The ``(value, targets)`` ``node`` binds, or ``None`` if it binds nothing."""
    if isinstance(node, ast.Assign):
        return node.value, list(node.targets)
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return node.value, [node.target]
    if isinstance(node, ast.NamedExpr):
        return node.value, [node.target]
    return None


def followable_handles(module: ast.Module) -> set[int]:
    """The ``id()`` of every handle occurrence in ``module`` the walk can account for.

    Two spellings qualify. The object of an attribute read is the census's own
    input. ``self.coordinator = coordinator`` stores the handle under the name
    the walk already looks for, so its reads stay visible; any other target
    hides them.
    """
    followable: set[int] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Attribute) and is_the_handle(node.value):
            followable.add(id(node.value))
            continue
        binding = binding_of(node)
        if binding is None:
            continue
        value, targets = binding
        if is_the_handle(value) and all(is_the_handle(t) for t in targets):
            followable.update(id(occurrence) for occurrence in (value, *targets))
    return followable


def members_read_from(path: Path) -> set[str]:
    """Every attribute name ``path`` reads off the coordinator handle.

    Refuses any occurrence of the handle the walk cannot account for. An alias
    can be spelled many ways, so rather than chase each one the census fails on
    all of them and stays honest about what it saw.
    """
    module = ast.parse(path.read_text(encoding="utf-8"))
    followable = followable_handles(module)

    members: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, (ast.Name, ast.Attribute)):
            if is_the_handle(node) and id(node) not in followable:
                msg = f"{path.name}:{node.lineno} hides the handle from the census"
                raise AssertionError(msg)
            if isinstance(node, ast.Attribute) and is_the_handle(node.value):
                members.add(node.attr)
    return members


def provider_census() -> set[str]:
    """The port's surface as the provider actually uses it, read from source."""
    return set().union(*(members_read_from(path) for path in PROVIDER_SOURCES))


@pytest.fixture
def coordinator() -> FetchCoordinator:
    """A coordinator that is never started, so it makes no request."""
    return FetchCoordinator(transport=HttpxAsyncTransport())  # type: ignore[arg-type]


class TestPortDeclaration:
    def test_declares_exactly_the_members_the_provider_uses(self) -> None:
        """The port is a census, so recompute it rather than restate it here.

        Asserting against a hand-written list would only catch drift in the
        protocol. Walking the provider catches drift on either side: a member
        the provider starts reading and a member it stops reading both fail.
        """
        assert provider_census() == declared_members(FetchPort)

    @pytest.mark.parametrize(
        "source",
        [
            "port = provider.coordinator\nport.request_listing('a')\n",
            "self._port = coordinator\nself._port.request_listing('a')\n",
            "ports = [coordinator]\nports[0].request_listing('a')\n",
            "run(provider.coordinator)\n",
        ],
        ids=["local-name", "attribute", "container", "argument"],
    )
    def test_the_census_refuses_a_handle_it_cannot_follow(
        self, tmp_path: Path, source: str
    ) -> None:
        """An alias has too many spellings to chase, so the walk refuses each one."""
        path = tmp_path / "aliased.py"
        path.write_text(source, encoding="utf-8")
        with pytest.raises(AssertionError, match="hides the handle"):
            members_read_from(path)

    def test_the_census_allows_the_constructor_binding(self, tmp_path: Path) -> None:
        """Storing the handle under its own name keeps every later read visible."""
        path = tmp_path / "stored.py"
        path.write_text(
            "self.coordinator = coordinator\nself.coordinator.request_listing('a')\n",
            encoding="utf-8",
        )
        assert members_read_from(path) == {"request_listing"}

    def test_waitable_declares_only_a_bare_wait(self) -> None:
        assert declared_members(Waitable) == {"wait"}
        assert parameter_shape(Waitable.wait) == [
            ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty)
        ]

    def test_a_threading_event_is_waitable(self) -> None:
        assert isinstance(threading.Event(), Waitable)


class TestCoordinatorSatisfiesThePort:
    def test_instance_check_passes(self, coordinator: FetchCoordinator) -> None:
        assert isinstance(coordinator, FetchPort)

    @pytest.mark.parametrize(
        "name",
        sorted(
            name for name in declared_members(FetchPort) if name.startswith("request_")
        ),
    )
    def test_request_signature_matches(self, name: str) -> None:
        """Defaults are compared too.

        ``isinstance`` on a runtime-checkable protocol tests that the names are
        present and nothing more, and no type checker reads nab-python, so this
        is what holds the coordinator to the port. A hash argument defaulted on
        one side only would let a caller drop integrity checking at every site
        at once.
        """
        assert parameter_shape(getattr(FetchCoordinator, name)) == parameter_shape(
            getattr(FetchPort, name)
        )
