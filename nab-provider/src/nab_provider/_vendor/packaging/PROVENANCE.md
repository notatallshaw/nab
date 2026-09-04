# Vendored `packaging` snapshot

This directory is a copy of the `packaging` library, vendored because nab runs a
fork of it. The tree is pristine `pypa/packaging` at a pinned commit plus at
most one checked-in patch.

## Model

- `tasks/vendoring/packaging.pin` holds the upstream commit the tree is pinned
  to.
- `tasks/vendoring/packaging.patch`, when present, is the only divergence from
  pristine; the rebuild reapplies it after refreshing. With no patch file the
  tree is byte-identical to upstream at the pin. The patch carries:
  - `ranges.py`: `VersionRange.from_bounds`, `snap_bounds`,
    `release_intervals`, and `relation` with its `RangeRelation` result type
    and the member aliases the return paths bind; an `assume_sorted` keyword
    on `filter` and the `VersionRange._filter_sorted` it dispatches to;
    `is_subset` and `is_disjoint` answered by a direct walk over the interval
    lists instead of an intermediate range; the module-level helpers they need
    (`_relate_bounds`, `_subset_bounds`, `_disjoint_bounds`,
    `_bisect_predicate`, `_partition_indexes`, `_make_project`, `_check_order`,
    `_lattice_release`, `_release_boundary_point`); the `RangeRelation` and
    `SortedOrder` `__all__` entries; and the class-docstring lines naming them.
  - `_ranges.py`: an unbounded end canonicalizes its inclusivity, and
    `LowerBound.__gt__`, `LowerBound.__le__`, and `UpperBound.__gt__` are
    written out beside `functools.total_ordering`, which still derives the
    rest. Those three, `LowerBound.__lt__`, and `UpperBound.__lt__` order two
    versions by comparing their cached `Version._key_cache` tuples, and fall
    back to the version operators when either side has no key yet.
    `BoundaryVersion` carries a class-level `_key_cache = None` so a boundary
    operand takes that fallback without a type check.
  - `markers.py`: `prepare_environment` with its `__all__` entry, and the
    `Marker.evaluate_prepared` it feeds, so code evaluating many markers
    against one environment builds it once. `evaluate` is now the two of them
    composed. `_format_marker` tests for a marker item first and serialises it
    by unpacking its three nodes into an f-string instead of joining a list
    comprehension, which puts the `[[...]]` unwrap under the list branch.
    Upstream's opening `assert isinstance(marker, (list, tuple, str))` is
    dropped, so a value of any other type now returns unchanged instead of
    tripping the assertion. `Marker.__str__` holds its result in a
    `_serialized` slot, so the `__hash__` and `__eq__` built on it walk the
    node tree once per instance instead of once per call.
  - `_parser.py`: `process_python_str` returns the body between the token's
    quotes when that body is ASCII and holds no backslash, newline, carriage
    return or NUL, and calls `ast.literal_eval` otherwise. The `QUOTED_STRING`
    rule admits no string prefix, so a token takes the same value either way.
  - `specifiers.py` and `_tokenizer.py`: `Specifier._regex` compiles
    `_condensed_regex_str`, which is `_specifier_regex_str` with the
    `re.VERBOSE` whitespace and comments removed, under `re.IGNORECASE` alone.
    It carries no surrounding `\s*` and `Specifier.__init__` strips the string
    before matching it, so `DEFAULT_RULES["SPECIFIER"]` can be that same
    compiled object instead of a second compile of the same pattern.

  Upstream PRs are planned for the bound ordering, the direct subset and
  disjoint walks, `filter`'s `assume_sorted` fast path,
  `from_bounds`/`snap_bounds`/`release_intervals`, the prepared marker
  environment, and the marker-item serialisation. `relation` is not proposed
  yet: most of its win is available from the direct walks alone. The
  quoted-string slice, the `Marker.__str__` memo, and the condensed specifier
  pattern are not proposed anywhere yet.
- `tasks/vendor-packaging.sh` fetches `pypa/packaging` at the pin, replaces this
  package with the pristine `src/packaging/` tree plus the repo-root license
  texts, and reapplies the patch. `--check` rebuilds into a temp location and
  fails if the committed tree has drifted. PROVENANCE.md is nab's own and stays
  out of both the rebuild and the comparison.
- CI runs `tasks/vendor-packaging.sh --check` on pull requests and on main.

## Refreshing

1. Bump `tasks/vendoring/packaging.pin` to the new upstream commit.
2. Merge the two histories per file, three-way: the committed vendored file is
   "ours", pristine at the old pin is the base, pristine at the new pin is
   "theirs". `git merge-file` over the three is enough, since only the files
   the new pin touches need merging.
3. Regenerate the patch from that merged tree, and from the merged tree only.
   Copy it over pristine-at-the-new-pin in a throwaway git repo laid out at
   this directory's repo-relative path, then `git diff` and keep the result
   verbatim. Never hand-edit the patch and never resolve a conflict inside it;
   it is generated output.
4. Run `tasks/vendor-packaging.sh`, then the test suite.

Skipping the merge and diffing the old committed tree straight against the new
pin looks like it works and is wrong: the diff then carries deletions for
everything the new pin added, so the patch reverts the upstream change the bump
was for. `--check` still passes, because it only proves the committed
tree equals pristine plus the patch, and a reverting patch satisfies that.
Bumping `6f52c6b` to `58c6cd7` this way produced a patch that deleted
`VersionRange.to_specifier_set` and every helper behind it.

The merge auto-resolving is not evidence that it resolved correctly. Check it
by diffing pristine against the merged tree at both pins and comparing the two
diffs: nab's divergence should come out line for line identical, with only the
surrounding context lines moved.

The patch is the accumulated divergence and no single fork branch carries all
of it, so never rebuild it from a branch.

## Behaviour the current pin inherits

The pin moved 34 commits, `58c6cd70` to `ef91ddbe`. The behaviour changes in
that span reach beyond the files nab diverges in, so each was checked against
nab's call sites.

Reachable:

- `parse_tag` refuses an interpreter component that is not a Python identifier,
  and `parse_wheel_filename` reports that as `InvalidWheelFilename`. Its project
  name anchors with `\Z` rather than `$`, so a name ending in a newline is
  refused too. `nab_provider.tags.wheel_tag_set` reads every wheel filename
  through `parse_tag`, so `foo-1.0-3.7-none-any.whl` and
  `foo-1.0-py3.7-none-any.whl` yield no tags. `nab_index` decides separately
  which files are readable and was admitting those; its `_tag_triple_is_parseable`
  now makes the same check, and `TestAdmittedWheelsCarryTags` holds the two
  together across the next bump.
- `_build/runner.py` reads a built wheel's name through `parse_wheel_filename`
  without guarding it, so a backend emitting a filename the new rules refuse
  raises instead of returning a name.

Not reachable:

- `$` to `\Z` end-anchoring in `_tokenizer.py`. Only a string ending in exactly
  one newline changes outcome: `'foo>=1.0\n'` parsed before, raises now. nab
  reads `Requires-Dist` through `email.parser`, which strips the line
  terminator, and a folded header leaves the continuation's leading whitespace
  after the newline, so no value ends in one.
- `Requirement` raising `InvalidRequirement` where an invalid specifier used to
  escape as `InvalidSpecifier`. Both derive from `ValueError`, which is what
  every nab call site catches.
- `cpython_tags` and `compatible_tags` no longer falling back to
  `platform_tags()` when `platforms` is explicitly empty. nab passes
  `_platform_tags_for_spec`, which returns at least one tag for every platform
  id it knows.

pypa/packaging#1213 merged `Value.serialize`'s quote handling in the form nab
already carried, so the patch's `_parser.py` serialisation hunk is gone. It
still does not re-escape a backslash, so a value holding one does not survive a
`str()` round trip; pypa/packaging#1374 proposes the fix and this pin is behind
it. Its `ValueError` on a value holding both quote characters is unreachable:
nab never constructs a `Value`, and a parsed value cannot hold its own
delimiter.

`nab_index` used `[\w\d._]*` for the wheel name where the vendored copy has used
`[\w._]+` since before the previous pin, so `-1.0-py3-none-any.whl` parsed to an
empty project name in `nab_index` and was refused here. `nab_index` now carries
the same pattern.

On a build tag whose digit run passes CPython's int-from-string limit,
`parse_wheel_filename` raises `ValueError` out of `int()` rather than returning
or rejecting, so `nab_index` has no answer to match: it keeps the wheel, and
`nab_provider.tags` sorts an unconvertible build number lowest.

## License

`packaging` is dual-licensed under the Apache License 2.0 and the 2-Clause BSD
License. The LICENSE files here are the upstream texts; nothing is relicensed.

## Why vendor instead of depending on it

`packaging` 26.3 ships `ranges.py` and `_ranges.py`, so the public
`VersionRange` and the `SpecifierSet.to_range()` factory nab's PubGrub solver
depends on are released. The patch above is not, and nab depends on every module
in it.

## Removal plan

Vendoring ends when the patch is empty. Until then the pin moves forward and the
patch shrinks as pieces land upstream. The marker algebra was two thirds of it and
is gone: it ships as `nab-markersets`, which binds this tree when it is importable
and released `packaging` otherwise.

Once the patch is empty, delete this directory and reinstate `packaging` as a
normal dependency:

- Add `packaging>=<release>` back to `nab-provider/pyproject.toml`
  `[project].dependencies`.
- Search-replace `from nab_provider._vendor.packaging` -> `from packaging` and
  `import nab_provider._vendor.packaging` -> `import packaging` across the
  workspace.
- Drop `nab_provider._vendor.packaging` from `nab_markersets._packaging`'s
  `BACKENDS`, and retire the `nab-vendored-packaging` extra with it. That reach
  is by name at import time, so `tasks/check_boundaries.py` does not see it.
- Remove this `_vendor/` tree and the `tasks/vendoring/` files.
