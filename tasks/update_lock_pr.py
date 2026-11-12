"""Publish refreshed locks as one signed draft PR using the runner's GitHub CLI."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

ROOT = Path(__file__).resolve().parents[1]
LOCKS = ("pylock.dev.toml", ".github/requirements/pylock.build.toml")
BRANCH = "ci/update-locks"
TITLE = "Update development and build locks"
BODY = (
    "Refresh the development and build dependencies with the seven-day upload cutoff."
)
COMMIT = """mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid } }
}"""


def run(root: Path, *command: str, payload: Mapping[str, object] | None = None) -> str:
    """Run a CLI command, propagating failures without interpreting shell text."""
    result = subprocess.run(
        command,
        cwd=root,
        input=json.dumps(payload) if payload is not None else None,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def same_lock(before: bytes, after: bytes) -> bool:
    """Compare lock contents and validation records, ignoring refresh provenance."""
    documents = [tomllib.loads(content.decode()) for content in (before, after)]
    for document in documents:
        provenance = document.get("tool", {}).get("nab", {})
        provenance.pop("created-at", None)
        provenance.pop("command-line", None)
    return documents[0] == documents[1]


@contextlib.contextmanager
def signed_candidate(
    root: Path,
    repository: str,
    base: str,
    additions: list[dict[str, str]],
) -> Generator[str, None, None]:
    """Keep a verified candidate reachable until its caller has pushed it."""
    candidate = f"ci/lock-candidate-{uuid.uuid4().hex}"
    run(
        root,
        "gh",
        "api",
        "--method",
        "POST",
        f"repos/{repository}/git/refs",
        "--input",
        "-",
        payload={"ref": f"refs/heads/{candidate}", "sha": base},
    )
    try:
        # GitHub signs this commit without putting a private signing key on the runner.
        head = run(
            root,
            "gh",
            "api",
            "graphql",
            "--input",
            "-",
            "--jq",
            ".data.createCommitOnBranch.commit.oid",
            payload={
                "query": COMMIT,
                "variables": {
                    "input": {
                        "branch": {
                            "repositoryNameWithOwner": repository,
                            "branchName": candidate,
                        },
                        "expectedHeadOid": base,
                        "message": {"headline": TITLE},
                        "fileChanges": {"additions": additions},
                    }
                },
            },
        )
        verification = run(
            root,
            "gh",
            "api",
            f"repos/{repository}/commits/{head}",
            "--jq",
            r'.commit.verification | "\(.verified) \(.reason)"',
        )
        if verification != "true valid":
            message = f"refusing unverified update commit: {verification}"
            raise ValueError(message)
        run(root, "git", "fetch", "--no-tags", "origin", f"refs/heads/{candidate}")
        yield head
    finally:
        run(
            root,
            "gh",
            "api",
            "--method",
            "DELETE",
            f"repos/{repository}/git/refs/heads/{candidate}",
        )


def publish(root: Path, repository: str) -> bool:
    """Replace only the managed branch, refusing an update raced by another writer."""
    additions = []
    for path in LOCKS:
        before = run(root, "git", "show", f"HEAD:{path}").encode()
        after = (root / path).read_bytes()
        if not same_lock(before, after):
            additions.append(
                {"path": path, "contents": base64.b64encode(after).decode()}
            )
    if not additions:
        return False

    reference = f"refs/heads/{BRANCH}"
    remote = run(root, "git", "ls-remote", "--heads", "origin", reference)
    old_head = remote.split()[0] if remote else ""
    existing = run(
        root,
        "gh",
        "pr",
        "list",
        "--repo",
        repository,
        "--base",
        "main",
        "--head",
        BRANCH,
        "--state",
        "open",
        "--json",
        "number",
        "--jq",
        ".[].number",
    )
    base = run(root, "git", "rev-parse", "HEAD")
    with signed_candidate(root, repository, base, additions) as head:
        run(
            root,
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "credential.helper=!gh auth git-credential",
            "push",
            f"--force-with-lease={reference}:{old_head}",
            "origin",
            f"{head}:{reference}",
        )

    if not existing:
        run(
            root,
            "gh",
            "pr",
            "create",
            "--repo",
            repository,
            "--base",
            "main",
            "--head",
            BRANCH,
            "--draft",
            "--title",
            TITLE,
            "--body",
            BODY,
        )
    return True


def main() -> None:
    """Tell the next workflow job whether an update needs CI."""
    changed = publish(ROOT, os.environ["GH_REPO"])
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"changed={str(changed).lower()}\n")


if __name__ == "__main__":
    main()
