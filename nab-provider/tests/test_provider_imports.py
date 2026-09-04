"""A host can drive the provider without project expansion or marker algebra."""

from __future__ import annotations

import subprocess
import sys

_PROBE = """
import sys

for name in (
    "nab_markersets",
    "nab_provider.target",
    "nab_provider.marker_holds",
    "nab_provider.requirements_file",
    "nab_provider.resolver_inputs",
):
    sys.modules[name] = None

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.provider import Provider
from nab_provider.records import WheelFile
from nab_provider.testing import make_coordinator
from nab_resolver.resolver import Resolver

wheel = WheelFile(
    "demo-1.0-py3-none-any.whl", "https://index.test/demo.whl", "1.0",
    None, True, None,
)
metadata = (
    "Metadata-Version: 2.1\\nName: demo\\nVersion: 1.0\\n"
    "Provides-Extra: speed\\n"
    "Requires-Dist: absent; python_version < '2'\\n\\n"
)
port = make_coordinator([wheel], package="demo", metadata_text=metadata)
roots = {
    "demo": SpecifierSet("==1.0").to_range(),
    "demo[speed]": VersionRange.full(),
}
provider = Provider(port, root_requirements=roots, root_extras={("demo", "speed")})
resolver = Resolver(provider, range_type=VersionRange, root_version=Version("0"))
solution = resolver.solve(roots)
assert solution.pins == {"demo": Version("1.0"), "demo[speed]": Version("1.0")}
assert solution.edges == (("demo[speed]", "demo"),)
assert solution.roots == ("demo", "demo[speed]")
"""


def test_solve_without_project_or_marker_algebra() -> None:
    """Resolve real metadata with extras and an environment-gated dependency."""
    subprocess.run(  # noqa: S603 - fixed interpreter and probe
        [sys.executable, "-c", _PROBE], check=True
    )
