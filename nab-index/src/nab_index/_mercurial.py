"""Acquire a pinned Mercurial source using an isolated client."""

from __future__ import annotations

__lazy_modules__ = (
    "functools",
    "os",
    "packaging.version",
    "pathlib",
    "shutil",
    "subprocess",
    "urllib.parse",
)

import os
import shutil
import subprocess
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from packaging.version import InvalidVersion, Version

from nab_provider.vcs_request import VcsCloneError

_MARKER = "nab-complete"
_CONTROL_TIMEOUT = 120
_PULL_TIMEOUT = 1800
_MINIMUM_VERSION = "7.2.4"
_NODE_TEMPLATE = "{node}"
_OUTPUT_COMMANDS = frozenset({"version", "log", "manifest"})

# These values locate the installed client, its OS facilities and trust roots.
_INHERITED_ENVIRONMENT = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
)

_CLIENT_OPTIONS = (
    "--noninteractive",
    "--config",
    "ui.clonebundles=false",
    "--config",
    "subrepos.allowed=false",
    "--config",
    "hostsecurity.minimumprotocol=tls1.2",
)


def clone_complete(dest: Path) -> bool:
    """Report whether the checkout was verified before publication."""
    return (dest / ".hg" / _MARKER).is_file()


def checkout(repo_url: str, node: str, dest: Path) -> None:
    """Pull the specified changeset into fresh storage and check it out."""
    env = _isolated_environment()
    executable = _find_executable(env)
    _check_version(executable, env)

    dest = dest.resolve()
    # A client started after update still starts outside the downloaded tree.
    cwd = dest.parent
    _run(executable, cwd, env, "init", str(dest))
    run = partial(_run, executable, cwd, env, repository=dest)

    command = "pull"
    config = ""
    if urlsplit(repo_url).scheme == "https":
        command = "nab-pull"
        extension = Path(__file__).with_name("_hg_no_redirect.py").resolve()
        config = f"extensions.nab_no_redirect={extension}"

    run(
        command,
        "--rev",
        node,
        "--",
        repo_url,
        extra_config=config,
        timeout=_PULL_TIMEOUT,
    )

    pinned_node = node.encode("ascii")
    fetched = run("log", "--rev", node, "--template", _NODE_TEMPLATE)
    if fetched != pinned_node:
        msg = "Mercurial fetched a different changeset than requested"
        raise VcsCloneError(msg)

    manifest = run("manifest", "--rev", node, "--template", "{type}\\0{path}\\0")
    links = _manifest_links(manifest)

    run("update", "--clean", "--rev", node)
    checked = run("log", "--rev", ".", "--template", _NODE_TEMPLATE)
    if checked != pinned_node:
        msg = "Mercurial checked out a different changeset than requested"
        raise VcsCloneError(msg)
    _check_links(dest, links)

    try:
        (dest / ".hg" / _MARKER).touch()
    except OSError as exc:
        msg = f"could not mark the Mercurial checkout complete: {exc}"
        raise VcsCloneError(msg) from exc


def _check_version(executable: str, env: dict[str, str]) -> None:
    """Require a released client with the audited configuration behavior."""
    # Older clients may still discover an ancestor's repository configuration.
    root = Path(Path(executable).anchor)
    raw = _run(executable, root, env, "version", "--template", "{ver}")
    msg = f"Mercurial source acquisition requires hg {_MINIMUM_VERSION} or newer"
    try:
        version = Version(raw.decode("ascii").strip())
    except (UnicodeDecodeError, InvalidVersion):
        raise VcsCloneError(msg) from None
    if version < Version(_MINIMUM_VERSION) or version.is_prerelease:
        raise VcsCloneError(msg)


def _manifest_links(manifest: bytes) -> list[bytes]:
    """Reject subrepositories before checkout and identify tracked symlinks."""
    records = manifest.split(b"\0")
    if records.pop() or len(records) % 2:
        msg = "Mercurial returned an invalid manifest"
        raise VcsCloneError(msg)

    fields = iter(records)
    links = []
    for kind, name in zip(fields, fields, strict=True):
        if name.rstrip(b" .").lower() in {b".hgsub", b".hgsubstate"}:
            msg = "Mercurial changesets with subrepositories are unsupported"
            raise VcsCloneError(msg)
        if kind == b"@":
            links.append(name)
    return links


def _check_links(dest: Path, links: list[bytes]) -> None:
    """Check that each materialized link stays within the source tree."""
    if not links:
        return
    root = dest.resolve()
    for name in links:
        relative = os.fsdecode(name)
        link = root / relative
        # Mercurial writes link targets as plain files on hosts without symlinks.
        if not link.is_symlink():
            continue
        msg = f"unsafe link in Mercurial source: {relative!r}"
        try:
            if link.readlink().anchor or not link.resolve(strict=True).is_relative_to(
                root
            ):
                raise VcsCloneError(msg)
        except (OSError, RuntimeError):
            raise VcsCloneError(msg) from None


def _isolated_environment() -> dict[str, str]:
    """Keep runtime paths while disabling system, user and repository config."""
    env = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _INHERITED_ENVIRONMENT
    }
    # A launcher using /usr/bin/env must also ignore relative PATH entries.
    env["PATH"] = os.pathsep.join(
        entry
        for entry in env.get("PATH", "").split(os.pathsep)
        if Path(entry).is_absolute()
    )
    env.update(
        HGRCPATH="", HGRCSKIPREPO="1", HGPLAIN="+strictflags", HGENCODING="utf-8"
    )
    return env


def _find_executable(env: dict[str, str]) -> str:
    """Select a Mercurial executable from absolute PATH entries."""
    # Older Python versions do not apply PATHEXT to absolute paths.
    name = "hg.exe" if os.name == "nt" else "hg"
    if env["PATH"]:
        for entry in env["PATH"].split(os.pathsep):
            executable = shutil.which(str(Path(entry) / name))
            if executable is not None:
                return executable

    msg = "Mercurial source requires an hg executable on PATH"
    raise VcsCloneError(msg)


def _run(
    executable: str,
    cwd: Path,
    env: dict[str, str],
    command: str,
    *arguments: str,
    repository: Path | None = None,
    extra_config: str = "",
    timeout: int = _CONTROL_TIMEOUT,
) -> bytes:
    """Run one controlled client operation with bounded time and no prompt."""
    action = "HTTPS pull" if command == "nab-pull" else command
    options = ("--config", extra_config) if extra_config else ()
    repo_option = ("--repository", str(repository)) if repository is not None else ()
    reads_output = command in _OUTPUT_COMMANDS
    stdout = subprocess.PIPE if reads_output else subprocess.DEVNULL
    try:
        result = subprocess.run(  # noqa: S603 - executable and options selected by nab
            (executable, *_CLIENT_OPTIONS, *options, *repo_option, command, *arguments),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )
    except subprocess.TimeoutExpired:
        msg = f"Mercurial {action} timed out after {timeout} seconds"
        raise VcsCloneError(msg) from None
    except subprocess.CalledProcessError as exc:
        msg = f"Mercurial {action} failed with exit status {exc.returncode}"
        raise VcsCloneError(msg) from None
    except OSError as exc:
        msg = f"could not run Mercurial {action}: {exc}"
        raise VcsCloneError(msg) from exc
    return result.stdout if reads_output else b""
