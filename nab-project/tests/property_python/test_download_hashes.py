"""Property tests for :mod:`nab_project.download` hash verification.

Oracle: hashlib over the served bytes.  Invariants:

* a digest that does not equal the content's hex digest always raises
  DownloadError and never leaves the bad file in the output dir;
* the correct digest always verifies, and a second run skips the file
  (idempotence).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from nab_project.download import DownloadError, download_lock
from nab_project.lockfile import IndexPin, LockInput, TargetLock, WheelArtifact
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget

from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

ALGOS = ["sha256", "sha384", "sha512"]

HEX = "0123456789abcdef"

# A resolve always runs against at least one target, so one entry is the
# smallest lock there is.
_HOST = ResolveTarget.for_declared(
    python_version="3.11", spec=PlatformSpec("linux_x86_64")
)


@dataclass
class _FakeResponse:
    content: bytes
    status_code: int = 200

    @property
    def headers(self) -> Mapping[str, str]:
        return {}

    @property
    def text(self) -> str:
        return self.content.decode()

    def json(self) -> object:
        return None

    def raise_for_status(self) -> None:
        pass


class _FakeTransport:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self._responses = responses

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        return _FakeResponse(content=self._responses[url])

    async def aclose(self) -> None:
        pass


def _lock_input(algo: str, digest: str) -> tuple[LockInput, str]:
    url = "https://example.invalid/foo-1.0-py3-none-any.whl"
    wheel = WheelArtifact(
        filename="foo-1.0-py3-none-any.whl",
        url=url,
        hashes=((algo, digest),),
    )
    pin = IndexPin(name="foo", version="1.0", index="pypi", sdist=None, wheels=(wheel,))
    lock = TargetLock(target=_HOST, pins={"foo": pin})
    return LockInput(targets={_HOST.label: lock}), url


@st.composite
def content_and_corruption(draw: st.DrawFn) -> tuple[bytes, str, str]:
    data = draw(st.binary(min_size=0, max_size=64))
    algo = draw(st.sampled_from(ALGOS))
    good = hashlib.new(algo, data).hexdigest()
    mutation = draw(st.sampled_from(["flip", "truncate", "extend", "empty"]))
    if mutation == "flip":
        pos = draw(st.integers(0, len(good) - 1))
        replacement = draw(st.sampled_from(HEX))
        assume(replacement != good[pos])
        bad = good[:pos] + replacement + good[pos + 1 :]
    elif mutation == "truncate":
        bad = good[:-2]
    elif mutation == "extend":
        bad = good + "ab"
    else:
        bad = ""
    return data, algo, bad


@PROPERTY_SETTINGS
@given(payload=content_and_corruption())
def test_wrong_digest_always_raises(
    payload: tuple[bytes, str, str], tmp_path_factory: pytest.TempPathFactory
) -> None:
    data, algo, bad_digest = payload
    out = tmp_path_factory.mktemp("dl")
    lock, url = _lock_input(algo, bad_digest)
    with pytest.raises(DownloadError):
        download_lock(lock, _FakeTransport({url: data}), out)
    assert not (out / "foo-1.0-py3-none-any.whl").exists(), "bad file left on disk"


@PROPERTY_SETTINGS
@given(data=st.binary(min_size=0, max_size=64), algo=st.sampled_from(ALGOS))
def test_correct_digest_verifies_and_is_idempotent(
    data: bytes, algo: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    out = tmp_path_factory.mktemp("dl")
    digest = hashlib.new(algo, data).hexdigest()
    lock, url = _lock_input(algo, digest)
    first = download_lock(lock, _FakeTransport({url: data}), out)
    assert [p.name for p in first.written] == ["foo-1.0-py3-none-any.whl"]
    assert (out / "foo-1.0-py3-none-any.whl").read_bytes() == data
    second = download_lock(lock, _FakeTransport({url: data}), out)
    assert second.written == ()
    assert [p.name for p in second.skipped] == ["foo-1.0-py3-none-any.whl"]
