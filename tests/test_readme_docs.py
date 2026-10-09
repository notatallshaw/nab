"""Build-policy routing, parsed examples and documentation links."""

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

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
INDEX = ROOT / "docs" / "index.md"
TUTORIAL = ROOT / "docs" / "tutorial" / "getting-started.md"
USE_THE_LOCK = ROOT / "docs" / "how-to" / "use-the-lock.md"
ARCHIVE_SOURCES = ROOT / "docs" / "how-to" / "archive-sources.md"
VCS_GUIDE = ROOT / "docs" / "how-to" / "vcs.md"

SITE_PAGES = (INDEX, TUTORIAL, USE_THE_LOCK, ARCHIVE_SOURCES)

PINNED_URL = f"git+https://github.com/myorg/pkg.git@{'0' * 40}"

TABLE_KINDS = {
    "local-sources": "local",
    "vcs-sources": "vcs",
    "archive-sources": "archive",
}


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

_FENCE = re.compile(
    r"^```(?P<language>\w*)\n(?P<body>.*?)^```", re.DOTALL | re.MULTILINE
)
_MARKDOWN_LINK = re.compile(r"\[[^]]+\]\((?P<target>[^)#]+\.md)(?:#[^)]*)?\)")
_STABLE_DOCS_ROOT = "https://nab.readthedocs.io/en/stable/"
_STABLE_DOCS_LINK = re.compile(rf"{re.escape(_STABLE_DOCS_ROOT)}[^)\s>]*")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _heading_slugs(path: Path) -> set[str]:
    """Return the URL slugs for Markdown headings in ``path``."""
    slugs: set[str] = set()
    fenced = False

    for line in _text(path).splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"^#{1,6} ", line):
            heading = line.lstrip("#").strip()
            slug = re.sub(r"-+", "-", re.sub(r"[^\w]+", "-", heading.casefold()))
            slugs.add(slug.strip("-"))

    return slugs


def _section(path: Path, title: str) -> str:
    """Return a level-two section, including any nested headings."""
    body: list[str] = []
    found = False
    fenced = False

    for line in _text(path).splitlines():
        if not fenced and line.startswith("## "):
            heading = line.removeprefix("## ").replace("`", "").strip()
            heading = heading.removesuffix(" (default)")
            if found:
                break
            found = heading == title
            continue

        if found:
            body.append(line)
        if line.startswith("```"):
            fenced = not fenced

    assert found, f"{path.relative_to(ROOT)} has no {title!r} section"
    return "\n".join(body).strip()


def _fenced_blocks(text: str, language: str) -> list[str]:
    """Return fenced blocks of one language in source order."""
    return [
        match["body"].rstrip()
        for match in _FENCE.finditer(text)
        if match["language"] == language
    ]


def _documented_vcs_default() -> VcsConfig:
    """Parse the default VCS block through nab's configuration registry."""
    blocks = _fenced_blocks(_section(VCS_GUIDE, "Default posture"), "toml")
    assert len(blocks) == 1, "the default VCS section must contain one TOML block"

    spec = next(option for option in OPTIONS if option.key == "vcs")
    table = tomli.loads(blocks[0])["tool"]["nab"]["vcs"]
    return spec.parse(table, where="docs/how-to/vcs.md")


def test_documented_vcs_default_is_the_shipped_default() -> None:
    """The documented VCS block matches every shipped default."""
    assert _documented_vcs_default() == VcsConfig()


def test_documented_vcs_default_refuses_a_pinned_url() -> None:
    """The documented default refuses even a commit-pinned URL."""
    with pytest.raises(UnsupportedVcsError, match=r'vcs\.policy is "block"'):
        admit_vcs_url(PINNED_URL, _documented_vcs_default())


def _admitted_kinds(policy: BuildPolicy, tree: Path) -> frozenset[str]:
    """Return source kinds that policy sends to a backend."""
    kinds: set[str] = set()
    for kind in sorted(set(TABLE_KINDS.values())):
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
    """Return the source kinds each successive policy starts building."""
    (tmp_path / "pyproject.toml").write_text(DYNAMIC_PYPROJECT, encoding="utf-8")
    monkeypatch.setattr(
        build_backend, "extract_metadata", lambda *args, **kwargs: BUILT
    )

    admitted = {policy: _admitted_kinds(policy, tmp_path) for policy in BuildPolicy}
    additions: dict[BuildPolicy, frozenset[str]] = {}
    below: frozenset[str] = frozenset()
    for policy, kinds in admitted.items():
        assert kinds >= below, "build policies must nest from strictest to loosest"
        additions[policy] = kinds - below
        below = kinds
    return additions


def test_build_policy_admits_source_routes(
    admitted_additions: dict[BuildPolicy, frozenset[str]],
) -> None:
    assert admitted_additions == {
        BuildPolicy.NEVER: frozenset(),
        BuildPolicy.BUILD_LOCAL: frozenset({"local"}),
        BuildPolicy.BUILD_REMOTE: frozenset({"vcs", "archive"}),
    }


def test_workspace_member_uses_local_build_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workspace discovery reaches the local-source build policy."""
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

    assert admitting == {BuildPolicy.BUILD_LOCAL, BuildPolicy.BUILD_REMOTE}


@pytest.fixture
def index_sdist_builders(monkeypatch: pytest.MonkeyPatch) -> frozenset[BuildPolicy]:
    """Return policies that send a dynamic index sdist to a backend."""
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


def test_only_build_remote_builds_index_sdists(
    index_sdist_builders: frozenset[BuildPolicy],
) -> None:
    assert index_sdist_builders == {BuildPolicy.BUILD_REMOTE}


def test_readme_project_is_valid_toml() -> None:
    """The example project is a minimal PEP 621 project."""
    blocks = _fenced_blocks(_section(README, "Example project"), "toml")
    assert len(blocks) == 1

    project = tomli.loads(blocks[0])["project"]
    assert project == {
        "name": "example",
        "version": "0.1.0",
        "dependencies": ["starlette<=0.36.0", "fastapi<=0.115.2"],
    }


def test_readme_stable_documentation_links_exist_locally() -> None:
    """Every stable documentation URL names a page built from this tree."""
    missing: list[str] = []
    for url in _STABLE_DOCS_LINK.findall(_text(README)):
        relative, _, fragment = url.removeprefix(_STABLE_DOCS_ROOT).partition("#")
        page = (
            INDEX
            if not relative
            else ROOT / "docs" / f"{relative.removesuffix('.html')}.md"
        )
        if not page.is_file() or (fragment and fragment not in _heading_slugs(page)):
            missing.append(url)

    assert missing == []


@pytest.mark.parametrize("page", SITE_PAGES, ids=lambda path: path.name)
def test_relative_document_links_exist(page: Path) -> None:
    """Every linked Markdown page resolves from the page carrying it."""
    missing = [
        target
        for target in _MARKDOWN_LINK.findall(_text(page))
        if not (page.parent / target).is_file()
    ]
    assert missing == []
