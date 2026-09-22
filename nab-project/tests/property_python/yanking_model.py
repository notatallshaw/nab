"""Enumerate legal preparation sequences without the provider's permission proofs."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from hypothesis import strategies as st

from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version

SPECS = (
    "",
    "==1",
    "==2",
    "===1.0",
    "==1+local",
    "==1.*",
    ">=1,<=1",
    ">=2",
    "!=1",
    ">=1,==1",
)
PIN_SPECS = frozenset({"==1", "==2", "===1.0", "==1+local", ">=1,==1"})
SPECIFIERS = {text: SpecifierSet(text) for text in SPECS}
VERSIONS = ("1", "2", "1.0", "1+local")
MARKERS = {
    "always": "",
    "linux": '; sys_platform == "linux"',
    "win32": '; sys_platform == "win32"',
    "extra": '; extra == "x"',
}
Key = tuple[str, str, bool]


@dataclass(frozen=True, order=True)
class Node:
    """A package or explicitly requested extra in the model graph."""

    name: str
    extra: bool = False

    @property
    def text(self) -> str:
        return self.name + ("[x]" if self.extra else "")


@dataclass(frozen=True)
class Request:
    """Keep declared pin syntax separate from its version-matching semantics."""

    name: str
    spec: str = ""
    extra: bool = False
    marker: str = "always"

    @property
    def text(self) -> str:
        return (
            self.name + ("[x]" if self.extra else "") + self.spec + MARKERS[self.marker]
        )

    @property
    def nodes(self) -> tuple[Node, ...]:
        return (
            (Node(self.name), Node(self.name, extra=True))
            if self.extra
            else (Node(self.name),)
        )

    def active(self, platform: str, *, extra: bool = False) -> bool:
        return self.marker in ("always", platform) or (self.marker == "extra" and extra)

    def matches(self, artifact: Artifact) -> bool:
        return self.name == artifact.name and SPECIFIERS[self.spec].contains(
            Version(artifact.version), prereleases=True
        )


@dataclass(frozen=True)
class Artifact:
    """One live or withdrawn file, including aliases at the same parsed version."""

    name: str
    version: str
    withdrawn: bool
    dependencies: tuple[Request, ...] = ()

    @property
    def key(self) -> Key:
        return self.name, self.version, self.withdrawn

    @property
    def url(self) -> str:
        return f"https://example.test/{self.name}-{self.version}-{self.withdrawn}.whl"


@dataclass(frozen=True)
class Case:
    """A bounded index snapshot and the original declarations for one target."""

    artifacts: tuple[Artifact, ...]
    roots: tuple[Request, ...]
    constraints: tuple[Request, ...] = ()
    platform: str = "linux"

    def declarations(
        self, selection: dict[str, Artifact], read: frozenset[Node]
    ) -> tuple[Request, ...]:
        """Expose inputs and only the metadata read by a model preparation sequence."""
        requests = [r for r in self.roots if r.active(self.platform)]
        requests.extend(
            request
            for node in sorted(read)
            for request in selection[node.name].dependencies
            if request.active(self.platform, extra=node.extra)
        )
        return tuple(requests)

    def permitted(
        self,
        selection: dict[str, Artifact],
        nodes: frozenset[Node],
        read: frozenset[Node],
    ) -> frozenset[Node]:
        """Evaluate one preparation step from declarations, not cached proof state."""
        requests = self.declarations(selection, read)
        demanded = {node for request in requests for node in request.nodes}
        pins = [r for r in requests if r.spec in PIN_SPECS]
        pins.extend(
            r
            for r in self.constraints
            if r.spec in PIN_SPECS and r.active(self.platform)
        )
        return frozenset(
            node
            for node in nodes & demanded
            if not selection[node.name].withdrawn
            or any(pin.matches(selection[node.name]) for pin in pins)
        )

    def reachable(
        self,
        selection: dict[str, Artifact],
        nodes: frozenset[Node],
        available: frozenset[Node],
    ) -> frozenset[Node]:
        """Enumerate every legal metadata-read prefix in a finite state space."""
        pending = [frozenset()]
        visited: set[frozenset[Node]] = set()
        admitted: set[Node] = set()
        while pending:
            read = pending.pop()
            if read in visited:
                continue
            visited.add(read)
            permitted = self.permitted(selection, nodes, read)
            admitted.update(permitted)
            pending.extend(read | {node} for node in permitted & available - read)
        return frozenset(admitted)


@dataclass(frozen=True)
class Solution:
    """A complete original dependency graph, without synthetic permission edges."""

    files: frozenset[Key]
    nodes: frozenset[Node]

    def extends(
        self, selection: dict[Node, Artifact], *, except_name: str | None = None
    ) -> bool:
        return all(
            artifact.name == except_name
            or (node in self.nodes and artifact.key in self.files)
            for node, artifact in selection.items()
        )


def solutions(case: Case) -> frozenset[Solution]:
    """Check all file assignments and all preparation prefixes for the small graph."""
    names = sorted({item.name for item in case.artifacts})
    choices = [
        (None, *(item for item in case.artifacts if item.name == name))
        for name in names
    ]
    valid = set()
    for values in product(*choices):
        selected = {
            name: item
            for name, item in zip(names, values, strict=True)
            if item is not None
        }
        nodes = frozenset(
            Node(name, extra) for name in selected for extra in (False, True)
        )
        prepared = case.reachable(selected, nodes, nodes)
        requests = case.declarations(selected, prepared)
        demanded = frozenset(node for req in requests for node in req.nodes)
        if demanded != prepared or {n.name for n in demanded} != set(selected):
            continue
        active = [
            *requests,
            *(
                r
                for r in case.constraints
                if r.name in selected and r.active(case.platform)
            ),
        ]
        if all(
            req.name in selected and req.matches(selected[req.name]) for req in active
        ):
            valid.add(
                Solution(frozenset(item.key for item in selected.values()), demanded)
            )
    return frozenset(valid)


def requests(
    names: tuple[str, ...], *, constraints: bool = False
) -> st.SearchStrategy[Request]:
    return st.builds(
        Request,
        name=st.sampled_from(names),
        spec=st.sampled_from(SPECS),
        extra=st.just(value=False) if constraints else st.booleans(),
        marker=st.sampled_from(tuple(MARKERS)),
    )


@st.composite
def cases(draw: st.DrawFn) -> Case:
    """Generate at most three packages and four artifact choices per package."""
    names = ("a", "b", "c")[: draw(st.integers(1, 3))]
    artifacts = []
    for name in names:
        versions = draw(
            st.lists(st.sampled_from(VERSIONS), min_size=1, max_size=2, unique=True)
        )
        for version in versions:
            states = draw(st.sampled_from(((False,), (True,), (False, True))))
            for withdrawn in states:
                dependencies = draw(st.lists(requests((*names, "missing")), max_size=2))
                artifacts.append(
                    Artifact(name, version, withdrawn, tuple(dependencies))
                )
    first = draw(requests(names))
    roots = (
        Request(first.name, first.spec, first.extra),
        *draw(st.lists(requests(names), max_size=1)),
    )
    constraints = tuple(draw(st.lists(requests(names, constraints=True), max_size=1)))
    return Case(
        tuple(artifacts), roots, constraints, draw(st.sampled_from(("linux", "win32")))
    )


def preference_parents(
    case: Case,
    selected: dict[Node, Artifact],
    available: frozenset[Node],
    target: str,
) -> dict[Node, Artifact]:
    """Find selected declaring ancestors grounded without the target's metadata."""
    selection = {node.name: item for node, item in selected.items()}
    external = frozenset(node for node in available if node.name != target)
    grounded = case.reachable(selection, frozenset(selected), external) & external
    edges = {
        node: {
            child
            for request in selected[node].dependencies
            if request.active(case.platform, extra=node.extra)
            for child in request.nodes
        }
        for node in grounded
    }
    ancestors = {Node(target), Node(target, extra=True)}
    while True:
        enlarged = ancestors | {
            node for node, children in edges.items() if children & ancestors
        }
        if enlarged == ancestors:
            return {node: selected[node] for node in grounded & ancestors}
        ancestors = enlarged


def live_completions(
    case: Case,
    valid: frozenset[Solution],
    selected: dict[Node, Artifact],
    available: frozenset[Node],
    target: str,
    inherited: frozenset[str] = frozenset(),
) -> frozenset[Solution]:
    """Change independent versions without turning existing live choices into yanks."""
    protected = (
        inherited
        | {item.name for item in selected.values() if not item.withdrawn}
        | {target}
    )
    fixed = preference_parents(case, selected, available, target)
    return frozenset(
        solution
        for solution in valid
        if any(
            name == target and not withdrawn for name, _, withdrawn in solution.files
        )
        and solution.extends(fixed)
        and not any(
            name in protected and withdrawn for name, _, withdrawn in solution.files
        )
    )


def assert_preferred(case: Case, actual: Solution, valid: frozenset[Solution]) -> None:
    """Reject a final yank when a scoped live completion remains possible."""
    artifacts = {item.key: item for item in case.artifacts}
    by_name = {key[0]: artifacts[key] for key in actual.files}
    selected = {node: by_name[node.name] for node in actual.nodes}
    for name, _, withdrawn in actual.files:
        if withdrawn:
            assert not live_completions(
                case, valid, selected, frozenset(selected), name
            ), (case, actual, name)
