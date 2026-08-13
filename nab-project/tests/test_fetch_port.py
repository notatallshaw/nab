"""Tests for the fetch port: what it declares, and who satisfies it."""

from __future__ import annotations

import ast
import inspect
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_project._testing.coordinator_fake import FakeFetchPort, make_coordinator
from nab_project.fetch import FetchCoordinator
from nab_provider.fetch_port import FetchPort, Waitable

SRC = Path(__file__).resolve().parents[1] / "src" / "nab_project"
PROVIDER_SRC = (
    Path(__file__).resolve().parents[2] / "nab-provider" / "src" / "nab_provider"
)

PROVIDER_SOURCES = (
    PROVIDER_SRC / "provider.py",
    *sorted((PROVIDER_SRC / "_provider").glob("*.py")),
)
"""The modules the census reads for the ``coordinator`` handle.

A module outside this list can read the handle unseen, so the list is part of
what the census asserts."""

HOST_SOURCES = (SRC / "_build_remote.py", SRC / "_sources.py")
"""The host-side halves of source materialisation and the remote build.

They hold the port under the name ``port``, so the census walks them for that
handle rather than for ``coordinator``."""


def declared_members(protocol: type) -> frozenset[str]:
    """The names a protocol declares, ignoring what Protocol itself adds."""
    return frozenset(name for name in vars(protocol) if not name.startswith("_"))


def parameter_shape(func: object) -> list[tuple[str, inspect._ParameterKind, object]]:
    """The name, kind and default of each parameter, in declaration order."""
    return [
        (param.name, param.kind, param.default)
        for param in inspect.signature(func).parameters.values()  # type: ignore[arg-type]
    ]


def is_the_handle(node: ast.expr, handle: str) -> bool:
    """Whether ``node`` evaluates to the handle named ``handle``.

    Two spellings reach it: the parameter of that name, and the attribute it
    is stored under.
    """
    if isinstance(node, ast.Name):
        return node.id == handle
    return isinstance(node, ast.Attribute) and node.attr == handle


def binding_of(node: ast.AST) -> tuple[ast.expr, Sequence[ast.expr]] | None:
    """The ``(value, targets)`` ``node`` binds, or ``None`` if it binds nothing."""
    if isinstance(node, ast.Assign):
        return node.value, list(node.targets)
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return node.value, [node.target]
    if isinstance(node, ast.NamedExpr):
        return node.value, [node.target]
    return None


def followable_handles(module: ast.Module, handle: str) -> set[int]:
    """The ``id()`` of every handle occurrence in ``module`` the walk can account for.

    A handle read as ``<handle>.<member>`` is the census's own input.
    ``self.coordinator = coordinator`` stores it under the name the walk already
    looks for, so its later reads stay visible; any other target hides them.
    ``port=port`` passes it on under its own name, keeping the callee's reads
    visible too.
    """
    followable: set[int] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Attribute) and is_the_handle(node.value, handle):
            followable.add(id(node.value))
            continue
        if isinstance(node, ast.Call):
            followable.update(
                id(kw.value)
                for kw in node.keywords
                if kw.arg == handle and is_the_handle(kw.value, handle)
            )
            continue
        binding = binding_of(node)
        if binding is None:
            continue
        value, targets = binding
        if is_the_handle(value, handle) and all(
            is_the_handle(t, handle) for t in targets
        ):
            followable.update(id(occurrence) for occurrence in (value, *targets))
    return followable


def members_read_from(path: Path, handle: str = "coordinator") -> set[str]:
    """Every attribute name ``path`` reads off the ``handle`` it holds.

    Refuses any occurrence of the handle the walk cannot account for: an alias
    has too many spellings to chase.
    """
    module = ast.parse(path.read_text(encoding="utf-8"))
    followable = followable_handles(module, handle)

    members: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, (ast.Name, ast.Attribute)):
            if is_the_handle(node, handle) and id(node) not in followable:
                msg = f"{path.name}:{node.lineno} hides the handle from the census"
                raise AssertionError(msg)
            if isinstance(node, ast.Attribute) and is_the_handle(node.value, handle):
                members.add(node.attr)
    return members


def provider_census() -> set[str]:
    """The port's surface as the provider uses it, read from source."""
    return set().union(*(members_read_from(path) for path in PROVIDER_SOURCES))


def host_census() -> set[str]:
    """The port's surface as the host-side helpers use it, read from source."""
    return set().union(
        *(members_read_from(path, handle="port") for path in HOST_SOURCES)
    )


@pytest.fixture
def coordinator() -> FetchCoordinator:
    """A coordinator that is never started, so it makes no request."""
    return FetchCoordinator(transport=HttpxAsyncTransport())  # type: ignore[arg-type]


class TestPortDeclaration:
    def test_declares_exactly_the_members_its_consumers_use(self) -> None:
        """The port is a census, so recompute it rather than restate it here."""
        assert provider_census() | host_census() == declared_members(FetchPort)

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

    def test_the_census_allows_a_keyword_pass_under_its_own_name(
        self, tmp_path: Path
    ) -> None:
        """Passing the handle as ``port=port`` keeps the callee's reads visible."""
        path = tmp_path / "passed.py"
        path.write_text(
            "build(request, port=port)\nport.request_direct_archive('a', 'b', 'c')\n",
            encoding="utf-8",
        )
        assert members_read_from(path, handle="port") == {"request_direct_archive"}

    def test_waitable_declares_only_a_bare_wait(self) -> None:
        assert declared_members(Waitable) == {"wait"}

        assert parameter_shape(Waitable.wait) == [
            ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty)
        ]

    def test_a_threading_event_is_waitable(self) -> None:
        assert isinstance(threading.Event(), Waitable)


class TestRequestSignatures:
    """Both implementations take the port's parameters, defaults included.

    ``isinstance`` on a runtime-checkable protocol tests only that the names are
    present, and no type checker reads nab-project, so this is what holds either
    implementation to the port. Defaults are part of it: a hash argument
    required on one side only narrows the callable.
    """

    @pytest.mark.parametrize(
        "implementation", [FetchCoordinator, FakeFetchPort], ids=["coordinator", "fake"]
    )
    @pytest.mark.parametrize(
        "name",
        sorted(
            name for name in declared_members(FetchPort) if name.startswith("request_")
        ),
    )
    def test_matches_the_port(self, implementation: type, name: str) -> None:
        assert parameter_shape(getattr(implementation, name)) == parameter_shape(
            getattr(FetchPort, name)
        )


class TestCoordinatorSatisfiesThePort:
    def test_instance_check_passes(self, coordinator: FetchCoordinator) -> None:
        assert isinstance(coordinator, FetchPort)


class TestFakeSatisfiesThePort:
    def test_instance_check_passes(self) -> None:
        assert isinstance(make_coordinator(), FetchPort)

    def test_a_name_the_port_does_not_have_raises(self) -> None:
        """A mock answers a name nobody defined; a class does not."""
        port = make_coordinator()
        with pytest.raises(AttributeError):
            port.request_listings("pkg")  # type: ignore[attr-defined]

    def test_an_archive_request_needs_its_arguments(self) -> None:
        """A request that stores bytes must refuse a call that omits them."""
        port = make_coordinator()

        with pytest.raises(TypeError):
            port.request_sdist_archive("pkg")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            port.request_direct_archive("pkg")  # type: ignore[call-arg]

    def test_a_request_hands_back_a_waitable(self) -> None:
        assert isinstance(make_coordinator().request_listing("pkg"), Waitable)

    def test_the_archive_requests_serve_what_they_were_given(self) -> None:
        """Both archive requests land bytes in the index, not just a set event."""
        port = make_coordinator(sdist_archive=b"archive-bytes")

        port.request_sdist_archive("pkg", "1.0", "https://ex.com/pkg-1.0.tar.gz")
        assert port.index.get_sdist_archive("pkg", "1.0") == b"archive-bytes"

        digest = "a" * 64
        port.request_direct_archive("pkg", digest, "https://ex.com/pkg-1.0.tar.gz")
        assert port.index.get_sdist_archive("pkg", digest) == b"archive-bytes"


class TestFakeCallRecord:
    def test_calls_are_recorded_in_order(self) -> None:
        port = make_coordinator()
        port.request_listing("a")
        port.request_listing("b")

        assert port.calls_to("request_listing") == [("a", False), ("b", False)]

    def test_reset_forgets_them(self) -> None:
        port = make_coordinator()
        port.request_listing("a")
        port.reset()

        assert not port.calls_to("request_listing")

    def test_reset_keeps_the_overrides(self) -> None:
        """A test installs an override, then resets so it counts only later calls."""
        port = make_coordinator()
        served = threading.Event()
        port.override("request_listing", lambda _package, _speculative: served)
        port.reset()

        assert port.request_listing("a") is served

    def test_an_override_replaces_the_answer_and_is_still_recorded(self) -> None:
        port = make_coordinator()
        served = threading.Event()
        port.override("request_listing", lambda _package, _speculative: served)

        assert port.request_listing("a") is served
        assert port.calls_to("request_listing") == [("a", False)]

    def test_reading_a_name_that_is_not_a_request_raises(self) -> None:
        with pytest.raises(KeyError, match="not a fetch request"):
            make_coordinator().calls_to("request_listings")

    def test_overriding_a_name_that_is_not_a_request_raises(self) -> None:
        with pytest.raises(KeyError, match="not a fetch request"):
            make_coordinator().override("request_listings", threading.Event)
