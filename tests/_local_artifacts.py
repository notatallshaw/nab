"""Small archive and local Simple API builders for real CLI resolves."""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from typing import TYPE_CHECKING

from installer.utils import copyfileobj_with_hashing

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


def make_wheel_archive(name: str, version: str, metadata: str, tag: str) -> bytes:
    """Build an installable wheel with the metadata and tag under test."""
    directory = f"{name}-{version}.dist-info"
    record_path = f"{directory}/RECORD"
    purelib = "true" if tag.endswith("-none-any") else "false"
    members = {
        f"{name}/__init__.py": f'__version__ = "{version}"\n',
        f"{directory}/METADATA": metadata + "\n",
        f"{directory}/WHEEL": (
            f"Wheel-Version: 1.0\nRoot-Is-Purelib: {purelib}\nTag: {tag}\n"
        ),
    }

    content = io.BytesIO()
    record = io.StringIO(newline="")
    rows = csv.writer(record, lineterminator="\n")
    with zipfile.ZipFile(content, "w") as archive:
        for path, text in members.items():
            with archive.open(path, "w") as member:
                digest, size = copyfileobj_with_hashing(
                    io.BytesIO(text.encode("utf-8")), member, "sha256"
                )
            rows.writerow((path, f"sha256={digest}", size))

        rows.writerow((record_path, "", ""))
        with archive.open(record_path, "w") as member:
            member.write(record.getvalue().encode("utf-8"))
    return content.getvalue()


def publish_artifacts(index: Path, name: str, artifacts: Mapping[str, bytes]) -> None:
    """Write a hashed Simple API page and the artifacts it names."""
    directory = index / name
    directory.mkdir(parents=True, exist_ok=True)
    links = []
    for filename, payload in artifacts.items():
        (directory / filename).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        links.append(f'<a href="{filename}#sha256={digest}">{filename}</a>\n')
    (directory / "index.html").write_text("".join(links), encoding="utf-8")
