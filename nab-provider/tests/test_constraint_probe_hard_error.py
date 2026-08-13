"""The constraint-attribution probe must contain a hard metadata error.

``Provider.has_satisfying_version`` re-runs the real ``choose_version`` over the
un-narrowed range to decide whether a user constraint is what hid a version.
That range includes the versions the constraint clipped away, so look-ahead can
reach one whose metadata is a hard integrity error (a failed PEP 658 sidecar
hash, or a bare wheel whose full body fails its published hash).  The probe only
labels a ``NO_VERSIONS`` clause, so that error must not escape and abort the
resolve.

What the probe contains is bounded by what the error says.  A fault of the one
version is contained; a transient transport failure, which names the moment and
not the version, still escapes and aborts.

An aborted scan never reaches its flush, so the probe also has to drop the
rejections it queued before the error, or the next package's scan turns them
into incompatibilities.
"""

from __future__ import annotations

import pytest

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import (
    HttpError,
    MalformedSimpleResponseError,
    MetadataHashMismatchError,
    UnserveableUrlError,
    WheelHashMismatchError,
)
from nab_provider.provider import Provider
from nab_provider.records import WheelFile
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_coordinator
from nab_resolver.resolver import Resolver
from nab_resolver.types import Incompatibility

V = Version
_PY312 = ResolveTarget.for_host_python("3.12.0")


def _wheel(name: str, version: str, *, has_metadata: bool = True) -> WheelFile:
    return WheelFile(
        filename=f"{name}-{version}-py3-none-any.whl",
        url=f"https://example.com/{name}-{version}-py3-none-any.whl",
        version=version,
        requires_python=None,
        has_metadata=has_metadata,
        upload_time=None,
    )


def _tie_wheel(tag: str) -> WheelFile:
    filename = f"pkg-1.0-{tag}.whl"
    return WheelFile(
        filename=filename,
        url=f"https://example.com/{filename}",
        version="1.0",
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )


def _tie_meta(requires_dist: str) -> str:
    return f"Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\nRequires-Dist: {requires_dist}\n\n"


def _leftover_clauses(provider: Provider) -> list[Incompatibility[str, Version]]:
    """Return the clauses a later ``choose_version`` flush would hand the resolver."""
    provider._flush_pending_blocks()
    return provider.consume_pending_clauses()


class TestConstraintProbeContainsHardError:
    def test_excluded_version_hard_error_does_not_abort_resolve(self) -> None:
        """A broken sidecar on a constraint-excluded version stays out of play.

        ``baz==2.0`` needs ``foo`` whose only version (``3.0``) the constraint
        ``foo<3.0`` excludes, so ``baz==2.0`` is rejected for ``baz==1.0``.
        ``foo==3.0``'s sidecar fails its hash, but the constraint keeps that
        version out of the resolve, so its integrity is irrelevant.
        """
        listings = {
            "baz": [_wheel("baz", "2.0"), _wheel("baz", "1.0")],
            "foo": [_wheel("foo", "3.0")],
        }
        metadata = {
            "2.0": (
                "Metadata-Version: 2.1\nName: baz\nVersion: 2.0\nRequires-Dist: foo\n\n"
            ),
            "1.0": "Metadata-Version: 2.1\nName: baz\nVersion: 1.0\n\n",
        }
        coordinator = make_coordinator(listings=listings, metadata_by_version=metadata)
        coordinator.index.store_metadata_error(
            "foo", "3.0", MetadataHashMismatchError("metadata sha256 mismatch")
        )
        root_reqs = {"baz": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")

        pins = resolver.resolve(
            root_reqs, constraints={"foo": SpecifierSet("<3.0").to_range()}
        )

        assert pins == {"baz": V("1.0")}

    def test_excluded_version_wheel_hash_error_does_not_abort_resolve(self) -> None:
        """A bare wheel failing its published hash stays out of play too.

        Same shape as the sidecar case, but ``foo==3.0`` publishes no
        ``core-metadata``, so the probe reaches it through rung 4 and the
        full-body read fails the listing's digest.
        """
        listings = {
            "baz": [_wheel("baz", "2.0"), _wheel("baz", "1.0")],
            "foo": [_wheel("foo", "3.0", has_metadata=False)],
        }
        metadata = {
            "2.0": (
                "Metadata-Version: 2.1\nName: baz\nVersion: 2.0\nRequires-Dist: foo\n\n"
            ),
            "1.0": "Metadata-Version: 2.1\nName: baz\nVersion: 1.0\n\n",
        }
        coordinator = make_coordinator(
            listings=listings,
            metadata_by_version=metadata,
            range_error=WheelHashMismatchError(
                "wheel sha256 mismatch: expected 0, got 1"
            ),
        )
        root_reqs = {"baz": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")

        pins = resolver.resolve(
            root_reqs, constraints={"foo": SpecifierSet("<3.0").to_range()}
        )

        assert pins == {"baz": V("1.0")}

    def test_probe_contains_hard_error(self) -> None:
        """A failed sidecar hash inside the probe returns False, it does not raise."""
        listings = {"foo": [_wheel("foo", "3.0")]}
        coordinator = make_coordinator(listings=listings)
        coordinator.index.store_metadata_error(
            "foo", "3.0", MetadataHashMismatchError("metadata sha256 mismatch")
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)

        assert provider.has_satisfying_version("foo", VersionRange.full()) is False

    @pytest.mark.parametrize(
        "error",
        [
            UnserveableUrlError("HTTP 404 for https://example.com/foo-3.0.metadata"),
            MalformedSimpleResponseError("sidecar body is not valid UTF-8"),
        ],
    )
    def test_unserveable_sidecar_probe_returns_false(self, error: HttpError) -> None:
        """An advertised sidecar the index will not serve is absorbed.

        A sidecar the listing advertised and the index then answered a 404 for,
        or handed back a non-UTF-8 body for, is a fault of that one version.
        Over the un-narrowed probe range this is a version the constraint
        clipped away, so ``has_satisfying_version`` must return ``False`` rather
        than abort the resolve.
        """
        listings = {"foo": [_wheel("foo", "3.0")]}
        coordinator = make_coordinator(listings=listings)
        coordinator.index.store_metadata_error("foo", "3.0", error)
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)

        assert provider.has_satisfying_version("foo", VersionRange.full()) is False

    def test_transient_transport_failure_propagates_out_of_probe(self) -> None:
        """A 5xx that outlived the retry budget aborts, it is not absorbed.

        A bare ``HttpError`` names the moment, not the version.  Reading it as
        "this version has no satisfying candidate" would let the resolve carry
        on and pin a different-but-valid answer, so it must escape the probe.
        """
        listings = {"foo": [_wheel("foo", "3.0")]}
        coordinator = make_coordinator(listings=listings)
        coordinator.index.store_metadata_error(
            "foo", "3.0", HttpError("HTTP 503 for https://example.com/foo-3.0.metadata")
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)

        with pytest.raises(HttpError, match="HTTP 503"):
            provider.has_satisfying_version("foo", VersionRange.full())

    def test_tie_divergence_on_probed_version_returns_false(self) -> None:
        """A tie divergence reached only by the probe returns False, not crash.

        The probe re-runs ``choose_version`` to attribute an already-failing
        resolve.  A version whose tie-ranked wheels declare divergent deps is a
        hard error like a failed integrity check, so it must stay out of play
        rather than abort the resolve.  The genuine crash still fires when the
        version is actually pinned outside a probe.
        """
        wheel_a = _tie_wheel("py2.py3-none-any")
        wheel_b = _tie_wheel("py3-none-any")
        coordinator = make_coordinator([wheel_a, wheel_b], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _tie_meta("alpha>=1"), metadata_url=wheel_a.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg", "1.0", _tie_meta("beta>=1"), metadata_url=wheel_b.metadata_url
        )
        root_reqs = {"pkg": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)

        found = provider.has_satisfying_version("pkg", VersionRange.full())

        assert found is False


class TestConstraintProbeDropsItsRejections:
    def test_decision_rejection_before_a_hard_error_is_dropped(self) -> None:
        """A candidate rejected before the error leaves no clause behind.

        ``foo==3.0`` needs ``bar<2`` against a decided ``bar==2.0``, so the
        scan queues a decision block and moves on to ``foo==2.0``, whose
        sidecar fails its hash.  The error skips the flush that would have
        emptied the queue, so the probe has to drop it.
        """
        listings = {
            "foo": [_wheel("foo", "3.0"), _wheel("foo", "2.0")],
            "bar": [_wheel("bar", "2.0")],
        }
        metadata = {
            "3.0": (
                "Metadata-Version: 2.1\nName: foo\nVersion: 3.0\n"
                "Requires-Dist: bar<2\n\n"
            )
        }

        coordinator = make_coordinator(listings=listings, metadata_by_version=metadata)
        coordinator.index.store_metadata_error(
            "foo", "2.0", MetadataHashMismatchError("metadata sha256 mismatch")
        )

        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("2.0")})

        assert provider.has_satisfying_version("foo", VersionRange.full()) is False

        assert _leftover_clauses(provider) == []

    def test_root_and_metadata_rejections_before_a_hard_error_are_dropped(self) -> None:
        """The permanent-rejection tables are dropped as well.

        ``foo==3.0`` needs ``bar<2`` against the root requirement ``bar>=2``
        and ``foo==2.5`` has no readable metadata, so the scan queues a root
        block and a metadata block before ``foo==2.0`` raises.  Flushed, those
        two would ban every ``foo`` above ``2.0`` for the rest of the resolve.
        """
        listings = {
            "foo": [
                _wheel("foo", "3.0"),
                _wheel("foo", "2.5", has_metadata=False),
                _wheel("foo", "2.0"),
            ],
            "bar": [_wheel("bar", "2.0")],
        }
        metadata = {
            "3.0": (
                "Metadata-Version: 2.1\nName: foo\nVersion: 3.0\n"
                "Requires-Dist: bar<2\n\n"
            )
        }

        coordinator = make_coordinator(listings=listings, metadata_by_version=metadata)
        coordinator.index.store_metadata_error(
            "foo", "2.0", MetadataHashMismatchError("metadata sha256 mismatch")
        )

        root_reqs = {
            "foo": VersionRange.full(admit_arbitrary=False),
            "bar": SpecifierSet(">=2").to_range(),
        }
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)

        assert provider.has_satisfying_version("foo", VersionRange.full()) is False

        assert _leftover_clauses(provider) == []
