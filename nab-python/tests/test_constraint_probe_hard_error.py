"""The constraint-attribution probe must contain a hard metadata error.

``Provider.has_satisfying_version`` re-runs the real ``choose_version`` over the
un-narrowed range to decide whether a user constraint is what hid a version.
That range includes the versions the constraint clipped away, so look-ahead can
reach one whose metadata is a hard integrity error (a failed PEP 658 sidecar
hash).  The probe only labels a ``NO_VERSIONS`` clause, so that error must not
escape and abort the resolve, and the snapshot the probe restores must survive
the failure path.
"""

from __future__ import annotations

from nab_index.client import MetadataHashMismatchError, WheelFile
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.specifiers import SpecifierSet
from nab_python._vendor.packaging.version import Version
from nab_python.provider import Provider
from nab_python.target import ResolveTarget
from nab_resolver.resolver import Resolver

V = Version
_PY312 = ResolveTarget.for_host_python("3.12.0")


def _wheel(name: str, version: str) -> WheelFile:
    return WheelFile(
        filename=f"{name}-{version}-py3-none-any.whl",
        url=f"https://example.com/{name}-{version}-py3-none-any.whl",
        version=version,
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )


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

    def test_probe_contains_hard_error_and_restores_snapshot(self) -> None:
        """A raising probe returns False and leaves the snapshot restored."""
        listings = {"foo": [_wheel("foo", "3.0")]}
        coordinator = make_coordinator(listings=listings)
        coordinator.index.store_metadata_error(
            "foo", "3.0", MetadataHashMismatchError("metadata sha256 mismatch")
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        sentinel = {"sentinel-blocker": ("x", V("1.0"))}
        provider._lookahead_aborted = dict(sentinel)

        found = provider.has_satisfying_version("foo", VersionRange.full())

        assert found is False
        assert provider._lookahead_aborted == sentinel
