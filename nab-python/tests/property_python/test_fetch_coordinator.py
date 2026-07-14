"""Property tests for :class:`nab_python.fetch.FetchCoordinator`.

Drives the real coordinator (real thread, real event loop, real queue)
with a fake async index client that completes requests in randomised
orders and injects errors.  Invariants:

* every submitted request's event is set within a modest timeout
  (provider-side waits are infinite ``event.wait()``, so a lost reply
  is a resolver deadlock);
* replies route to the right key (values encode pkg/version, no
  cross-talk);
* each logical fetch key reaches the client at most once (dedup),
  including duplicate submissions, batch duplicates, and the
  listing-triggered metadata prefetch;
* injected errors surface as replies (None / listing error), never
  as hangs.
"""

from __future__ import annotations

import asyncio
import threading
from collections import Counter

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from nab_index.client import SdistFile, WheelFile
from nab_python.fetch import FetchCoordinator

pytestmark = pytest.mark.property

PKGS = ["alpha", "beta", "gamma"]
VERSIONS = ["1.0", "2.0", "3.0"]
DELAYS = [0.0, 0.001, 0.003]

EVENT_TIMEOUT = 15.0

# Each example spins up a real fetcher thread, so the example budget
# stays small to keep the file fast.
COORDINATOR_SETTINGS = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

Key = tuple[str, ...]
Plan = dict[Key, tuple[str, float]]


def _wheel(pkg: str, version: str, *, has_metadata: bool) -> WheelFile:
    return WheelFile(
        filename=f"{pkg}-{version}-py3-none-any.whl",
        url=f"https://example.invalid/{pkg}-{version}-py3-none-any.whl",
        version=version,
        requires_python=None,
        has_metadata=has_metadata,
        upload_time=None,
    )


def _metadata_url(pkg: str, version: str) -> str:
    """The sidecar URL the listing publishes for ``(pkg, version)``."""
    return _wheel(pkg, version, has_metadata=True).metadata_url or ""


def _sdist(pkg: str, version: str) -> SdistFile:
    return SdistFile(
        filename=f"{pkg}-{version}.tar.gz",
        url=f"https://example.invalid/{pkg}-{version}.tar.gz",
        version=version,
        requires_python=None,
        upload_time=None,
    )


class FakeClient:
    """Stands in for the index client inside the fetcher loop."""

    def __init__(self, plan: Plan, listing_has_metadata: dict[str, bool]) -> None:
        self.plan = plan
        self.listing_has_metadata = listing_has_metadata
        self.calls: Counter[Key] = Counter()
        self.calls_lock = threading.Lock()
        self.closed = False

    async def _common(self, key: Key, plan_key: Key | None = None) -> None:
        """Count one call against ``key``; take its outcome from ``plan_key``.

        Metadata calls count against the sidecar URL, since two wheels of
        one version publish two sidecars and so are two logical fetches,
        while the plan still injects errors per ``(package, version)``.
        """
        with self.calls_lock:
            self.calls[key] += 1
        mode, delay = self.plan.get(
            plan_key if plan_key is not None else key, ("ok", 0.0)
        )
        if delay:
            await asyncio.sleep(delay)
        if mode == "err":
            msg = f"boom {key}"
            raise RuntimeError(msg)

    async def get_files(self, package: str) -> list[WheelFile | SdistFile]:
        await self._common(("listing", package))
        has_meta = self.listing_has_metadata.get(package, False)
        files: list[WheelFile | SdistFile] = [
            _wheel(package, v, has_metadata=has_meta) for v in VERSIONS
        ]
        files.append(_sdist(package, VERSIONS[0]))
        return files

    async def get_metadata_text(
        self,
        package: str,
        version: str,
        url: str,
        metadata_hash: tuple[str, str] | None = None,
    ) -> str:
        await self._common(
            ("metadata", package, version, url), ("metadata", package, version)
        )
        return f"META:{package}:{version}"

    async def get_sdist_files(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> tuple[str, str | None]:
        await self._common(("sdist", package, version))
        return (f"PKGINFO:{package}:{version}", f"PYPROJECT:{package}:{version}")

    async def get_sdist_archive(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> bytes:
        await self._common(("sdist-archive", package, version))
        return f"BYTES:{package}:{version}".encode()

    async def aclose(self) -> None:
        self.closed = True


class FakeClientCoordinator(FetchCoordinator):
    """Coordinator whose fetcher loop talks to a :class:`FakeClient`."""

    def __init__(self, fake_client: FakeClient) -> None:
        super().__init__(transport=None)  # type: ignore[arg-type]
        self._fake_client = fake_client

    def _build_client(self) -> FakeClient:  # type: ignore[override]
        return self._fake_client


# One action = (kind, pkg_idx, ver_idx).
actions = st.lists(
    st.tuples(
        st.sampled_from(["listing", "metadata", "sdist", "sdist_archive", "batch"]),
        st.integers(0, len(PKGS) - 1),
        st.integers(0, len(VERSIONS) - 1),
    ),
    min_size=1,
    max_size=12,
)

plans = st.dictionaries(
    st.tuples(
        st.sampled_from(["listing", "metadata", "sdist", "sdist-archive"]),
        st.sampled_from(PKGS),
        st.sampled_from(VERSIONS),
    ).map(lambda t: t if t[0] != "listing" else ("listing", t[1])),
    st.tuples(st.sampled_from(["ok", "err"]), st.sampled_from(DELAYS)),
    max_size=12,
)

listing_meta_flags = st.fixed_dictionaries({p: st.booleans() for p in PKGS})


@COORDINATOR_SETTINGS
@given(action_list=actions, plan=plans, has_meta=listing_meta_flags)
def test_every_request_gets_exactly_one_correct_reply(
    action_list: list[tuple[str, int, int]],
    plan: Plan,
    has_meta: dict[str, bool],
) -> None:
    client = FakeClient(plan, has_meta)
    requested: list[tuple[Key, threading.Event]] = []

    with FakeClientCoordinator(client) as coord:
        for kind, pi, vi in action_list:
            pkg, ver = PKGS[pi], VERSIONS[vi]
            if kind == "listing":
                ev = coord.request_listing(pkg)
                requested.append((("listing", pkg), ev))
            elif kind == "metadata":
                ev = coord.request_metadata(pkg, ver, _metadata_url(pkg, ver))
                requested.append((("metadata", pkg, ver), ev))
            elif kind == "sdist":
                ev = coord.request_sdist(
                    pkg, ver, f"https://example.invalid/{pkg}-{ver}.tar.gz"
                )
                requested.append((("sdist", pkg, ver), ev))
            elif kind == "sdist_archive":
                ev = coord.request_sdist_archive(
                    pkg, ver, f"https://example.invalid/{pkg}-{ver}.tar.gz"
                )
                requested.append((("sdist-archive", pkg, ver), ev))
            else:  # batch: include a duplicate pair on purpose
                items: list[tuple[str, str, str, tuple[str, str] | None]] = [
                    (pkg, ver, _metadata_url(pkg, ver), None),
                    (pkg, ver, _metadata_url(pkg, ver), None),
                ]
                for bpkg, bver, bev in coord.request_metadata_batch(items):
                    requested.append((("metadata", bpkg, bver), bev))

        # Liveness: every reply must arrive.
        for key, ev in requested:
            assert ev.wait(EVENT_TIMEOUT), f"no reply for {key}"

        index = coord.index

        # Routing + error-as-reply checks per key.
        meta_keys = {k for k, _ in requested if k[0] == "metadata"}
        sdist_keys = {k for k, _ in requested if k[0] == "sdist"}
        for key in meta_keys | sdist_keys:
            _, pkg, ver = key
            assert index.has_metadata(pkg, ver), f"no metadata slot for {key}"
            value = index.get_metadata(pkg, ver)
            allowed: set[str | None] = set()
            base_ver = ver.split("#", 1)[0]
            if ("metadata", pkg, ver) in meta_keys:
                mode, _ = client.plan.get(("metadata", pkg, ver), ("ok", 0.0))
                allowed.add(f"META:{pkg}:{ver}" if mode == "ok" else None)
            # The listing prefetch can also have written META for this slot.
            if has_meta.get(pkg) and "#" not in ver:
                mode, _ = client.plan.get(("metadata", pkg, ver), ("ok", 0.0))
                allowed.add(f"META:{pkg}:{ver}" if mode == "ok" else None)
            if ("sdist", pkg, ver) in sdist_keys:
                mode, _ = client.plan.get(("sdist", pkg, base_ver), ("ok", 0.0))
                allowed.add(f"PKGINFO:{pkg}:{ver}" if mode == "ok" else None)
            assert value in allowed, f"cross-talk at {key}: {value!r} not in {allowed}"

        for key, _ in requested:
            if key[0] == "sdist-archive":
                _, pkg, ver = key
                mode, _delay = client.plan.get(key, ("ok", 0.0))
                got = index.get_sdist_archive(pkg, ver)
                expected = f"BYTES:{pkg}:{ver}".encode() if mode == "ok" else None
                assert got == expected, f"archive mismatch for {key}"

        for key, _ in requested:
            if key[0] == "listing":
                pkg = key[1]
                mode, _delay = client.plan.get(("listing", pkg), ("ok", 0.0))
                listing = index.get_listing(pkg)
                if mode == "ok":
                    assert listing is not None
                    assert len(listing) == len(VERSIONS) + 1
                    assert all(f.filename.startswith(pkg) for f in listing), (
                        f"listing cross-talk for {pkg}"
                    )
                else:
                    assert listing is None
                    assert isinstance(index.get_listing_error(pkg), RuntimeError)

    # Dedup: every fetch key reached the client at most once, even with
    # duplicate submissions, batch duplicates, and prefetch overlap.
    dupes = {k: c for k, c in client.calls.items() if c > 1}
    assert not dupes, f"duplicate fetches: {dupes}"
    assert client.closed
