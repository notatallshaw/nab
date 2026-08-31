"""Check the README's build and VCS policy sections against the code.

The README is the ``nab`` distribution's PyPI description, so it states the
default posture to readers who never open the documentation site.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import tomli

from nab.config.ladder import OPTIONS
from nab_project import _sources as sources
from nab_project import build_backend
from nab_project._testing.coordinator_fake import make_coordinator
from nab_project.workspace import read_workspace_members
from nab_provider._provider import build_remote, metadata_resolver
from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import SourceBuildPolicyError, UnsupportedSdistError
from nab_provider.metadata import WheelMetadata
from nab_provider.policy import BuildPolicy, SourceRequest
from nab_provider.provider import Provider
from nab_provider.records import SdistFile
from nab_provider.vcs_admission import UnsupportedVcsError, VcsConfig, admit_vcs_url

README = Path(__file__).resolve().parents[1] / "README.md"

PINNED_URL = f"git+https://github.com/myorg/pkg.git@{'0' * 40}"

# The kind each config source table's entries reach the build gate as.
TABLE_KINDS = {
    "local-sources": "local",
    "vcs-sources": "vcs",
    "archive-sources": "archive",
}

WORKSPACE_MEMBERS = "workspace members"

# Every way of declaring a source, spelled as the README spells it, and the
# kind it reaches the build gate as.  Declared, not derived: the table names
# are checked against the registry below and the member row against discovery,
# but each table's own kind is a literal in ``nab_project._sources``.
SOURCE_ROUTES = {
    **{f"[[tool.nab.{key}]]": kind for key, kind in TABLE_KINDS.items()},
    WORKSPACE_MEMBERS: "local",
}

INDEX_SDIST_PHRASE = "sdists from an index"

DYNAMIC_PYPROJECT = '[project]\nname = "pkg"\ndynamic = ["dependencies"]\n'

INDEX_SDIST = SdistFile(
    filename="pkg-1.0.tar.gz",
    url="https://example.com/pkg-1.0.tar.gz",
    version="1.0",
    requires_python=None,
    upload_time=None,
)

DYNAMIC_SDIST_METADATA = WheelMetadata(
    name="pkg", version=Version("1.0"), dynamic=frozenset({"Requires-Dist"})
)

BUILT = WheelMetadata(name="pkg", version=Version("1.0"))


def _section(title: str) -> str:
    """The README body under ``## <title>``, up to the next heading.

    A heading only counts outside a fenced block, so a ``#`` comment in a
    toml example does not cut the section short.
    """
    body = README.read_text(encoding="utf-8").partition(f"\n## {title}\n")[2]
    assert body, f"README.md has no {title} section"

    lines: list[str] = []
    fenced = False
    for line in body.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#"):
            break
        lines.append(line)

    return "\n".join(lines)


def _documented_default() -> VcsConfig:
    """The VCS section's toml block, parsed by the registry's own parser."""
    block = re.search(r"```toml\n(.*?)\n```", _section("VCS policy"), re.DOTALL)
    assert block, "the VCS policy section has no toml block"

    spec = next(option for option in OPTIONS if option.key == "vcs")
    return spec.parse(tomli.loads(block[1])["tool"]["nab"]["vcs"], where="README.md")


def test_documented_default_is_the_shipped_default() -> None:
    """The block the section leads with matches ``VcsConfig``, key for key."""
    assert _documented_default() == VcsConfig()


def test_documented_default_refuses_a_pinned_url() -> None:
    """A commit-pinned URL is refused under the block, as the section says."""
    with pytest.raises(UnsupportedVcsError, match=r'vcs\.policy is "block"'):
        admit_vcs_url(PINNED_URL, _documented_default())


def _build_policy_bullets() -> dict[BuildPolicy, str]:
    """The Build policy section's bullets, keyed by the level each opens."""
    bullets = re.findall(
        r"^ \* ([a-z-]+)[^:\n]*:(.*?)(?=^ \*|\Z)",
        _section("Build policy"),
        re.MULTILINE | re.DOTALL,
    )
    documented = {BuildPolicy(token): body for token, body in bullets}
    assert set(documented) == set(BuildPolicy), "a build policy has no bullet"
    return documented


def _documented_routes() -> dict[BuildPolicy, frozenset[str]]:
    """The ways of declaring a source each level's bullet is the first to name.

    A bullet may restate a stricter level's sources to say what its own level
    adds to them, so a route counts only for the first bullet that names it,
    levels taken strictest first. Index sdists are not declared in config and
    have a test of their own.
    """
    bullets = _build_policy_bullets()

    routes: dict[BuildPolicy, frozenset[str]] = {}
    named_above: set[str] = set()
    for policy in BuildPolicy:
        named = {phrase for phrase in SOURCE_ROUTES if phrase in bullets[policy]}
        routes[policy] = frozenset(named - named_above)
        named_above |= named
    return routes


def _admitted_kinds(policy: BuildPolicy, tree: Path) -> frozenset[str]:
    """The source kinds ``policy`` lets through to a backend, over ``tree``."""
    kinds: set[str] = set()
    for kind in sorted(set(SOURCE_ROUTES.values())):
        try:
            metadata = sources.extract_source_metadata(
                tree,
                descriptor=f"{kind} source 'pkg'",
                policy=policy,
                kind=kind,
                offline=True,
                build_config=None,
            )
        except SourceBuildPolicyError:
            continue
        assert metadata is BUILT
        kinds.add(kind)
    return frozenset(kinds)


@pytest.fixture
def admitted_additions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[BuildPolicy, frozenset[str]]:
    """Per level, the source kinds it starts sending to a backend.

    Runs the gate over a tree whose static read yields nothing, once per
    kind and level, and reports each level's gain over the one below it.
    The backend is stubbed, so what is recorded is the policy decision and
    not whether a real build would work.
    """
    (tmp_path / "pyproject.toml").write_text(DYNAMIC_PYPROJECT, encoding="utf-8")
    monkeypatch.setattr(
        build_backend, "extract_metadata", lambda *args, **kwargs: BUILT
    )

    admitted = {policy: _admitted_kinds(policy, tmp_path) for policy in BuildPolicy}

    below: frozenset[str] = frozenset()
    additions: dict[BuildPolicy, frozenset[str]] = {}
    for policy, kinds in admitted.items():
        assert kinds >= below, "the levels are declared strictest first and nest"
        additions[policy] = kinds - below
        below = kinds
    return additions


def test_build_policy_bullets_name_the_declared_sources_each_level_adds(
    admitted_additions: dict[BuildPolicy, frozenset[str]],
) -> None:
    """Each bullet names every way of declaring the sources its level adds.

    Naming the kind is not enough: local-sources entries and workspace
    members share the ``local`` kind, so a bullet naming one of them reads
    as a rule about that one alone.
    """
    expected = {
        policy: frozenset(
            phrase for phrase, kind in SOURCE_ROUTES.items() if kind in kinds
        )
        for policy, kinds in admitted_additions.items()
    }
    assert _documented_routes() == expected


def test_every_source_table_the_registry_defines_has_a_route() -> None:
    """Every ``*-sources`` table the config registry defines has a row above.

    The bullets have to answer for each one, so a new table fails here rather
    than going unnamed in the README.
    """
    assert set(TABLE_KINDS) == {
        option.key for option in OPTIONS if option.key.endswith("-sources")
    }


def test_a_workspace_member_takes_the_route_its_row_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A discovered member reaches the build gate as the kind its row names.

    Every other row is spelled as the table its entries parse from; a member
    is declared by path, so the route from discovery to the gate is run here.
    """
    root = tmp_path / "pyproject.toml"
    root.write_text(
        '[project]\nname = "root"\n[tool.nab.workspace]\nmembers = ["member"]\n',
        encoding="utf-8",
    )
    member = tmp_path / "member"
    member.mkdir()
    (member / "pyproject.toml").write_text(DYNAMIC_PYPROJECT, encoding="utf-8")

    monkeypatch.setattr(
        build_backend, "extract_metadata", lambda *args, **kwargs: BUILT
    )

    (source,) = read_workspace_members(root)
    port = make_coordinator()

    admitting: set[BuildPolicy] = set()
    for policy in BuildPolicy:
        request = SourceRequest(
            package="pkg",
            source=source,
            build_policy=policy,
            vcs_cache_dir=None,
            archive_cache_dir=None,
            require_pin=False,
        )
        try:
            materialized = sources.materialize_source(port, request, None)
        except SourceBuildPolicyError:
            continue
        assert materialized.metadata is BUILT
        admitting.add(policy)

    assert admitting == {
        policy
        for policy in BuildPolicy
        if SOURCE_ROUTES[WORKSPACE_MEMBERS] in _admitted_kinds(policy, member)
    }


@pytest.fixture
def index_sdist_builders(monkeypatch: pytest.MonkeyPatch) -> frozenset[BuildPolicy]:
    """The levels whose gate sends an index sdist to a backend.

    Reconciles a dynamic-deps sdist with no static fallback, once per level
    and each on a fresh index, since the reconciled metadata is cached there
    and the next level would read it back. The build is stubbed, so what is
    recorded is the policy decision and not whether a real build would work.
    """
    monkeypatch.setattr(
        build_remote, "build_remote_sdist", lambda *args, **kwargs: BUILT
    )

    building: set[BuildPolicy] = set()
    for policy in BuildPolicy:
        provider = Provider(
            make_coordinator([INDEX_SDIST], package="pkg"), build_policy=policy
        )
        try:
            metadata = metadata_resolver.resolve_dynamic_sdist(
                provider, ("pkg", Version("1.0")), DYNAMIC_SDIST_METADATA
            )
        except UnsupportedSdistError:
            continue
        assert metadata is BUILT
        building.add(policy)
    return frozenset(building)


def test_only_the_level_that_builds_index_sdists_names_them(
    index_sdist_builders: frozenset[BuildPolicy],
) -> None:
    """The bullet naming index sdists is the level whose gate builds one."""
    naming = {
        policy
        for policy, body in _build_policy_bullets().items()
        if INDEX_SDIST_PHRASE in body
    }
    assert naming == index_sdist_builders
