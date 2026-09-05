# nab-provider

The Python packaging provider that drives
[`nab-resolver`](https://pypi.org/project/nab-resolver/), with no I/O of its
own: no socket, no filesystem, no subprocess, no clock.

It owns the mechanics of how the resolver interacts with standards based
Python package indexes and packages, and implements the build, distribution
and VCS policies that let a user control resolver and install behavior.

Everything it needs arrives through `nab_provider.fetch_port.FetchPort`, which
its host implements; `nab_provider.testing` ships one over a store you fill
yourself.

## When to use it

Use `nab-provider` to embed nab's resolution in a host that already owns its
networking and caching, keeping
[`nab-index`](https://pypi.org/project/nab-index/) out of the import graph.
Most users want [`nab`](https://pypi.org/project/nab/) instead.

The API is currently under rapid experimentation, if you use it
pin to an exact version.

## Candidate source ranges

`nab_provider.candidate_ranges` supplies `CandidateKey` and `CandidateRange` for hosts that prepare and order their own candidates. A key pairs a PEP 440 version with a host-defined source identifier, so installed and downloaded distributions can remain distinct at the same version.

Construct `CandidateRange(versions)` from a `VersionRange` supplied by nab-provider's vendored packaging to admit matching versions on every source. Use `CandidateRange.singleton(key)` for one candidate or `CandidateRange.for_source(source, versions)` to restrict a source. Intersection, union, subtraction and complement operate independently on each source, including identifiers not yet encountered by the host.

Leave the `prereleases` argument unset when constructing input ranges or specifier sets; explicitly configured `True` or `False` policies raise `ValueError`. Candidate ordering, source admission and prerelease selection remain host responsibilities.

The module imports only the provider's vendored packaging and the standard library.
