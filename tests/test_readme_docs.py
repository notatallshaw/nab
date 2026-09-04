"""Check nab's entry journeys and user-facing policy claims."""

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
BUILD_POLICY_REFERENCE = ROOT / "docs" / "reference" / "build-policy.md"

ENTRY_PAGES = (README, TUTORIAL)
SITE_PAGES = (INDEX, TUTORIAL, USE_THE_LOCK, ARCHIVE_SOURCES)

PINNED_URL = f"git+https://github.com/myorg/pkg.git@{'0' * 40}"

TABLE_KINDS = {
    "local-sources": "local",
    "vcs-sources": "vcs",
    "archive-sources": "archive",
}

WORKSPACE_MEMBERS = "workspace member"
BUILD_ROUTE_PHRASES = {
    "[[tool.nab.local-sources]]": "local",
    WORKSPACE_MEMBERS: "local",
    "vcs clones": "vcs",
    "archive sources": "archive",
}
INDEX_SDIST_PHRASE = "remote pypi sdists"

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
_REFERENCE_LINK = re.compile(r"\[[^]]+\]\[(?P<label>[^]]+)\]")
_REFERENCE_DEFINITION = re.compile(
    r"^\[(?P<label>[^]]+)\]: (?P<target>\S+)$", re.MULTILINE
)
_STABLE_DOCS_ROOT = "https://nab.readthedocs.io/en/stable/"
_STABLE_DOCS_LINK = re.compile(rf"{re.escape(_STABLE_DOCS_ROOT)}[^)\s>]*")
_WHY_DOCS_TARGETS = {
    f"{_STABLE_DOCS_ROOT}reference/build-policy.html",
    f"{_STABLE_DOCS_ROOT}how-to/archive-sources.html",
    f"{_STABLE_DOCS_ROOT}reference/configuration.html#the-resolve-environment",
    f"{_STABLE_DOCS_ROOT}reference/lockfile.html",
    f"{_STABLE_DOCS_ROOT}reference/lockfile.html#checking-the-lock-in-ci",
}


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


def _blocks(path: Path, language: str) -> list[str]:
    """Return fenced blocks of one language in page order."""
    return _fenced_blocks(_text(path), language)


def _commands(path: Path) -> str:
    """Return shell blocks with line continuations joined."""
    return "\n".join(re.sub(r"\\\n\s*", " ", block) for block in _blocks(path, "bash"))


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


def _build_policy_sections() -> dict[BuildPolicy, str]:
    """Return the three policy sections keyed by their runtime values."""
    return {
        policy: _section(BUILD_POLICY_REFERENCE, policy.value) for policy in BuildPolicy
    }


def _before(text: str, marker: str, *, section: str) -> str:
    """Return text before a required marker."""
    normalized = re.sub(r"\s+", " ", text.casefold())
    body, separator, _ = normalized.partition(marker)
    assert separator, f"the {section} section has no {marker!r} boundary"
    return body


def _documented_build_claims() -> dict[BuildPolicy, str]:
    """Return only each section's positive backend-permission claims."""
    sections = _build_policy_sections()
    never = re.sub(r"\s+", " ", sections[BuildPolicy.NEVER].casefold())
    assert "runs no build backend" in never

    return {
        BuildPolicy.NEVER: "",
        BuildPolicy.BUILD_LOCAL: _before(
            sections[BuildPolicy.BUILD_LOCAL],
            "remote pypi sdists",
            section=BuildPolicy.BUILD_LOCAL.value,
        ),
        BuildPolicy.BUILD_REMOTE: _before(
            sections[BuildPolicy.BUILD_REMOTE],
            "a backend failure",
            section=BuildPolicy.BUILD_REMOTE.value,
        ),
    }


def _documented_build_routes() -> dict[BuildPolicy, frozenset[str]]:
    """Return the source routes each section says start building."""
    return {
        policy: frozenset(phrase for phrase in BUILD_ROUTE_PHRASES if phrase in body)
        for policy, body in _documented_build_claims().items()
    }


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


def test_build_policy_sections_name_each_source_route_the_level_adds(
    admitted_additions: dict[BuildPolicy, frozenset[str]],
) -> None:
    """The documented route additions match the build-policy behavior."""
    expected = {
        policy: frozenset(
            phrase for phrase, kind in BUILD_ROUTE_PHRASES.items() if kind in kinds
        )
        for policy, kinds in admitted_additions.items()
    }
    assert _documented_build_routes() == expected


def test_every_source_table_the_registry_defines_is_documented() -> None:
    """The build-policy reference names every registered source table."""
    registered = {option.key for option in OPTIONS if option.key.endswith("-sources")}
    assert set(TABLE_KINDS) == registered

    text = _text(BUILD_POLICY_REFERENCE)
    documented = {key for key in TABLE_KINDS if f"[[tool.nab.{key}]]" in text}
    assert documented == registered


def test_workspace_member_takes_its_documented_build_route(
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

    documented_kind = BUILD_ROUTE_PHRASES[WORKSPACE_MEMBERS]
    assert admitting == {
        policy
        for policy in BuildPolicy
        if documented_kind in _admitted_kinds(policy, member)
    }


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


def test_only_the_level_that_builds_index_sdists_names_them(
    index_sdist_builders: frozenset[BuildPolicy],
) -> None:
    """The index-sdist claim belongs to exactly the policies that build one."""
    naming = {
        policy
        for policy, body in _documented_build_claims().items()
        if INDEX_SDIST_PHRASE in body
    }
    assert naming == index_sdist_builders


def test_readme_project_is_valid_toml() -> None:
    """The quick-start project is a minimal PEP 621 project."""
    blocks = _fenced_blocks(_section(README, "Quick start"), "toml")
    assert len(blocks) == 1

    project = tomli.loads(blocks[0])["project"]
    assert project == {
        "name": "example",
        "version": "0.1.0",
        "dependencies": ["starlette<=0.36.0", "fastapi<=0.115.2"],
    }


def test_readme_routes_each_reason_to_maintained_documentation() -> None:
    """The value proposition links to the pages that define its contracts."""
    definitions = dict(_REFERENCE_DEFINITION.findall(_text(README)))
    section = _section(README, "Why nab?")
    labels = _REFERENCE_LINK.findall(section)

    assert set(labels) <= definitions.keys()
    assert {definitions[label] for label in labels} == _WHY_DOCS_TARGETS


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


@pytest.mark.parametrize("page", ENTRY_PAGES, ids=lambda path: path.name)
def test_entry_page_completes_the_first_lock(page: Path) -> None:
    """Each entry path locks first, then hands the result to pip."""
    commands = _commands(page)
    lock = "nab lock pyproject.toml"
    install = "python -m pip install -r pylock.toml"

    assert lock in commands
    assert install in commands
    assert commands.index(lock) < commands.index(install)


@pytest.mark.parametrize("page", ENTRY_PAGES, ids=lambda path: path.name)
def test_entry_page_names_pip_pylock_support(page: Path) -> None:
    """The version floor and experimental boundary sit beside the command."""
    text = _text(page).lower()
    assert "pip 26.1" in text
    assert "pylock.toml" in text
    assert "experimental" in text


def test_lock_guide_carries_each_install_and_ci_workflow() -> None:
    """The guide carries the pylock, hashed, wheelhouse, and CI commands."""
    commands = _commands(USE_THE_LOCK)
    expected = (
        "python -m pip install -r pylock.toml",
        "nab lock --format requirements pyproject.toml",
        "python -m pip install --require-hashes -r requirements.txt",
        "python -m pip download --only-binary=:all: --require-hashes",
        "--dest wheelhouse -r requirements.txt",
        "python -m pip install --no-index --find-links wheelhouse",
        "--require-hashes -r requirements.txt",
        "nab lock --locked pyproject.toml",
    )
    for command in expected:
        assert command in commands


def test_lock_guide_scopes_pip_selection() -> None:
    """Pip's current pylock selector boundary is documented."""
    text = _text(USE_THE_LOCK).lower()
    for phrase in (
        "pip 26.1",
        "current interpreter and platform",
        "default dependency groups",
        "no extras",
    ):
        assert phrase in text


def test_lock_guide_does_not_call_nab_download_lock_consumption() -> None:
    """The guide distinguishes a fresh download resolve from consuming a file."""
    text = _text(USE_THE_LOCK)
    assert "resolves the project again" in text
    assert "does not read `pylock.toml`" in text


def test_archive_guide_locks_before_installing() -> None:
    """The archive task reaches a consumed lock after declaring its source."""
    commands = _commands(ARCHIVE_SOURCES)
    lock = "nab lock pyproject.toml"
    install = "python -m pip install -r pylock.toml"

    assert lock in commands
    assert install in commands
    assert commands.index(lock) < commands.index(install)


def test_index_carries_the_project_boundary() -> None:
    """A reader can judge the CLI before choosing a task."""
    text = _text(INDEX)
    for phrase in (
        "nab is experimental",
        "CPython 3.10 and newer",
        "Neither command installs packages",
        "Project-root `name @ git+...` requirements are not resolved yet",
    ):
        assert phrase in text


@pytest.mark.parametrize("page", SITE_PAGES, ids=lambda path: path.name)
def test_relative_document_links_exist(page: Path) -> None:
    """Every linked Markdown page resolves from the page carrying it."""
    missing = [
        target
        for target in _MARKDOWN_LINK.findall(_text(page))
        if not (page.parent / target).is_file()
    ]
    assert missing == []
