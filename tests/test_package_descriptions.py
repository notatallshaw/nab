"""Check each README's claims against the packages that implement them.

``project.readme`` is the whole of a distribution's page on PyPI.  A capability
the page credits to a library has to be implemented in that library, and one a
library's own page promises has to arrive with an install of it.
"""

from __future__ import annotations

import importlib.util
import re
from itertools import takewhile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomli

from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]

README = REPO_ROOT / "README.md"
NAB_PROJECT = REPO_ROOT / "nab-project"
NAB_PROJECT_README = NAB_PROJECT / "README.md"

# The module implementing each capability a README names.  A phrase is listed
# only when one module implements it, since the lookup answers with a single
# distribution.
CAPABILITY_MODULES = {
    "config ladder": "nab.config.ladder",
    "resolve orchestration": "nab_project.resolve",
    "workspace discovery": "nab_project.workspace",
    "build path": "nab_project.build_backend",
    "lockfile emitter": "nab_project.lockfile",
    "downloader": "nab_project.download",
}

# A Libraries bullet: the library it opens with, then everything it claims.
_BULLET = re.compile(r"^ \* `([^`\n]+)`:(.*?)(?=^ \*|\Z)", re.MULTILINE | re.DOTALL)


def _project(directory: Path) -> dict[str, Any]:
    """The ``[project]`` table of the check-out rooted at ``directory``."""
    text = (directory / "pyproject.toml").read_text(encoding="utf-8")
    return tomli.loads(text)["project"]


def _package_distributions() -> dict[str, str]:
    """Distribution name, keyed by each import package the workspace ships."""
    checkouts = [REPO_ROOT, *(p.parent for p in REPO_ROOT.glob("*/pyproject.toml"))]

    packages = {}
    for directory in checkouts:
        name = canonicalize_name(_project(directory)["name"])
        for package in (directory / "src").iterdir():
            if package.is_dir():
                packages[package.name] = name
    return packages


def _bullet_list(lines: Iterable[str]) -> str:
    """The bullet list in ``lines``, from its first bullet to its last line.

    A bullet's body runs on over indented lines, so the list ends at the first
    line that opens neither a bullet nor a continuation.
    """
    listed: list[str] = []
    for line in lines:
        if line.startswith(" * ") or (listed and line.startswith("   ")):
            listed.append(line)
        elif listed:
            break
    return "\n".join(listed)


def _library_bullets() -> dict[str, str]:
    """The Libraries section's bullet bodies, keyed by the library each names."""
    body = README.read_text(encoding="utf-8").partition("\n# Libraries\n")[2]
    assert body, "README.md has no Libraries section"

    section = takewhile(lambda line: not line.startswith("#"), body.splitlines())
    return {
        canonicalize_name(name): claims
        for name, claims in _BULLET.findall(_bullet_list(section))
    }


def _named_capabilities(text: str) -> dict[str, str]:
    """The distribution behind each capability ``text`` names."""
    packages = _package_distributions()
    return {
        phrase: packages[module.partition(".")[0]]
        for phrase, module in CAPABILITY_MODULES.items()
        if phrase in text
    }


def _install_provides(directory: Path) -> set[str]:
    """``directory``'s own distribution name, plus the ones it depends on."""
    project = _project(directory)

    provided = {canonicalize_name(project["name"])}
    provided.update(
        canonicalize_name(Requirement(requirement).name)
        for requirement in project["dependencies"]
    )
    return provided


def test_every_capability_names_a_module_the_workspace_ships() -> None:
    """The tests below resolve only the phrases their own text names.

    A mapping no README currently uses would go unchecked, so pin them all here.
    """
    packages = _package_distributions()
    outside = sorted(
        f"{phrase} -> {module}"
        for phrase, module in CAPABILITY_MODULES.items()
        if module.partition(".")[0] not in packages
    )
    assert not outside, f"capabilities mapped outside the workspace: {outside}"

    gone = sorted(
        f"{phrase} -> {module}"
        for phrase, module in CAPABILITY_MODULES.items()
        if importlib.util.find_spec(module) is None
    )
    assert not gone, f"the READMEs are checked against modules that are gone: {gone}"


def test_library_bullets_credit_the_package_that_holds_the_capability() -> None:
    """A capability the Libraries section gives a library is implemented there."""
    bullets = _library_bullets()
    assert bullets, "README.md's Libraries section has no bullets"

    named = {library: _named_capabilities(body) for library, body in bullets.items()}
    assert any(named.values()), "no Libraries bullet names a known capability"

    misplaced = sorted(
        f"{library} is credited with the {phrase}, which ships in {owner}"
        for library, capabilities in named.items()
        for phrase, owner in capabilities.items()
        if owner != library
    )
    assert not misplaced, f"README.md misattributes: {misplaced}"


def test_nab_project_promises_only_what_installing_it_brings_in() -> None:
    """What its page names has to arrive with nab-project or a dependency."""
    named = _named_capabilities(NAB_PROJECT_README.read_text(encoding="utf-8"))
    assert named, "nab-project/README.md names no known capability"

    provided = _install_provides(NAB_PROJECT)
    missing = sorted(
        f"the {phrase}, which ships in {owner}"
        for phrase, owner in named.items()
        if owner not in provided
    )
    assert not missing, (
        f"nab-project/README.md promises {missing}, and nab-project depends on "
        f"{sorted(provided - {'nab-project'})}"
    )
