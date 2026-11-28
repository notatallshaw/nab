# nab-candidates

Use `nab-candidates` when your installer already finds Python package files and reads their
metadata. Your finder supplies the files compatible with the requested Python and platform. The
library requests their dependency metadata as needed and chooses a compatible set. Your installer
can then fetch and install the selected files.

Nab uses this search when locking a project that may need a yanked file. The standalone library
requires Python 3.10 or later and depends on `nab-resolver` and `packaging`. To run the example from
the root of a nab checkout, install the two local distributions.

```sh
python -m pip install ./nab-resolver ./nab-candidates
```

## Two installs from the same index

Suppose an index offers `renderer` 1.9 and marks its 2.0 wheel as **yanked** (withdrawn). The only
release of `viewer` requires `renderer==2.0`.

```text
Requested                       Selected
renderer>=1                     renderer 1.9
viewer                          viewer 1.0
                                └── renderer 2.0 (yanked)
```

An exact pin from the user or a selected dependency can permit a yanked file. Here the solution for
`viewer` explains which dependency supplied that pin. A request for `renderer>=2` alone would have
no eligible file.

## Supply a catalogue

This catalogue has one compatible wheel per version. In an installer, `read_choices()` would consult
the finder and `read_candidate()` would read the chosen wheel's metadata.

```python
from packaging.ranges import VersionRange
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from nab_candidates.resolution import PreparedResolution
from nab_candidates.versions import (
    PreparedVersionCatalogue,
    VersionCandidate,
    VersionCandidateData,
    is_pin,
)


class Wheels(PreparedVersionCatalogue[VersionCandidate, VersionCandidateData]):
    range_type = VersionRange
    files = {"viewer": [("1.0", False)], "renderer": [("2.0", True), ("1.9", False)]}

    @staticmethod
    def base_name(package):
        return canonicalize_name(package)

    @staticmethod
    def pin_project(requirement):
        return canonicalize_name(requirement.name)

    def read_choices(self, package):
        return [
            VersionCandidate(package, Version(label), withdrawn=yanked)
            for label, yanked in self.files.get(package, ())
        ]

    def read_candidate(self, candidate):
        if candidate.package == "viewer":
            pin = Requirement("renderer==2.0")
            return VersionCandidateData({"renderer": pin.specifier.to_range()}, (pin,))

        return VersionCandidateData({}, ())
```

A root requirement asks the solver to include a package. A constraint narrows the allowed versions
if the package is needed. Pass exact pins alongside the ranges to preserve the original
declarations. With `try_live=True`, the solver tries live files first. A matching exact pin can
trigger a check before the solver reads a yanked file’s metadata.

```python
def resolve(text):
    root = Requirement(text)
    catalogue = Wheels(unavailable_reason="wheel metadata is unavailable")
    return PreparedResolution.from_catalogue(
        catalogue,
        roots={canonicalize_name(root.name): root.specifier.to_range()},
        input_pins=(root,) if is_pin(root) else (),
        constraints={},
        try_live=True,
    ).solve()


assert resolve("renderer>=1").candidates["renderer"].label == "1.9"

solution = resolve("viewer")
assert solution.candidates["renderer"].label == "2.0"
assert solution.edges == (("viewer", "renderer"),)
assert solution.admission_sources["renderer"] == ("viewer==1.0 requires renderer==2.0",)
```

`solve()` returns the selected candidates, roots, dependency edges and reasons for yanked-file
admission. Use `select()` when the installer only needs the candidate mapping.

## Use your installer's files

- `VersionCandidate` distinguishes the package, version label and yanked status. If two files with
  the same values have different sources or dependencies, include the artifact in your candidate
  identity. For an extra, the base candidate must identify the same artifact.
- Keep file listings stable for one resolution. The catalogue caches the listing and each prepared
  candidate's dependencies. If a newly discovered source changes the available files, start a new
  resolution with a fresh catalogue.
- Return `None` from `read_candidate()` when the metadata rules out that file. If the metadata could
  not be read, also add the candidate to `unavailable`. If a project listing is missing, add its
  name to `missing_listings`. This lets the resolver report when it lacks the evidence to rule out a
  file.
- Override `order_candidate_batch()` to apply the finder's ordering within a batch. The default
  tries newer versions first and preserves the finder’s order for equal versions.

See the
[embedding guide](https://nab.readthedocs.io/en/latest/how-to/embed-the-resolver.html#resolve-python-package-files)
for the catalogue interface and the lower-level preference search.
