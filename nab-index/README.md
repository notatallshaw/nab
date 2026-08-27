# nab-index

PyPI Simple-API client and on-disk cache used by
[`nab-project`](https://pypi.org/project/nab-project/) and
[`nab`](https://pypi.org/project/nab/).

It provides:

- A small async transport interface with two drop-in backends:
  - `urllib3` (default; pulled in by the base install).
  - `httpx` (extra: `nab-index[httpx]`).
- A Simple-API client with JSON and HTML decoders.
- A disk cache for project listings and file metadata responses.
- A multi-index router (ordered named indexes plus per-package
  overrides that pin a package to one index).
- A small VCS clone helper used by the higher-level VCS policy.

## When to use it

Use `nab-index` if you need a typed PyPI Simple-API client with an
on-disk cache.  Most users want
[`nab`](https://pypi.org/project/nab/) instead.

The API is currently under rapid experimentation, if you use it
pin to an exact version.
