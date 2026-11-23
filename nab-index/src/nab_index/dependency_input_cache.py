"""Cache a dependency declaration by its source archive and relative file name."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ._json_decode import decode_json
from .atomic import atomic_write_text

__all__ = ["BUCKET", "read_text", "verify_record", "write_text"]

BUCKET = "dependency-inputs-v1"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _record_key(archive_hash: str, relative: str) -> Path:
    """Return the record's location inside the dependency input bucket."""
    return Path(archive_hash) / (_digest(relative) + ".json")


def _decode(raw: bytes, archive_hash: str) -> tuple[str, str]:
    """Return the file name and text after checking their identity and checksum."""
    record = decode_json(raw.decode())
    if not isinstance(record, dict):
        msg = "dependency input record must be a JSON object"
        raise ValueError(msg)  # noqa: TRY004 - the serialized record is malformed.

    relative = record.get("path")
    text = record.get("content")
    if not isinstance(relative, str) or not isinstance(text, str):
        msg = "dependency input path and content must be strings"
        raise ValueError(msg)  # noqa: TRY004 - the serialized fields are malformed.

    # The record must belong to this archive and contain intact text.
    if record.get("archive-sha256") != archive_hash:
        msg = "dependency input does not match its cache location"
        raise ValueError(msg)
    if record.get("content-sha256") != _digest(text):
        msg = "dependency input checksum mismatch"
        raise ValueError(msg)

    return relative, text


def read_text(bucket: Path, archive_hash: str, relative: str) -> str | None:
    """Read the requested declaration if the cached record is usable."""
    key = _record_key(archive_hash, relative)
    try:
        stored_name, text = _decode((bucket / key).read_bytes(), archive_hash)
    except (OSError, ValueError):
        return None

    return text if stored_name == relative else None


def verify_record(key: Path, raw: bytes) -> None:
    """Check a record found in the bucket against its archive and file name."""
    archive_hash = key.parent.name
    relative, _ = _decode(raw, archive_hash)
    if re.fullmatch(r"[0-9a-f]{64}", archive_hash) is None or key != _record_key(
        archive_hash, relative
    ):
        msg = "dependency input does not match its cache location"
        raise ValueError(msg)


def write_text(bucket: Path, archive_hash: str, relative: str, text: str) -> None:
    """Store the declaration in the existing JSON record format."""
    path = bucket / _record_key(archive_hash, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(
            {
                "archive-sha256": archive_hash,
                "path": relative,
                "content": text,
                "content-sha256": _digest(text),
            }
        ),
    )
