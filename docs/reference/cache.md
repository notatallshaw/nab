# On-disk cache

nab caches PyPI Simple-API responses and wheel metadata on disk so a
repeated resolve reuses what it already fetched. The root defaults to
`~/.cache/nab` (or `$XDG_CACHE_HOME/nab`); `--cache-dir` and `--no-cache`
override it, and `--offline` serves from it without any network. Inspect
and reset it with [`nab cache`](cli.md).

## Layout

Each entry is one file under a versioned bucket directory. Bumping a
bucket's version suffix retires the old format: the stale directory is
harmless and `nab cache clear` reclaims it.

| Bucket | Holds |
| ------ | ----- |
| `simple-v0/` | the raw Simple-API JSON body and a `.policy` sidecar |
| `simple-parsed-v0/` | the parsed listing, an accelerator for the body |
| `simple-neg-v0/` | a short-lived record that a name returned a 404 |
| `metadata-v1/` | PEP 658 metadata and recovered wheel `METADATA`, immutable |
| `sdist-v1/` | an sdist's `PKG-INFO` and `pyproject.toml`, immutable |

Buckets are keyed per index, so two indexes never share an entry.

## Freshness

A listing follows a small subset of RFC 9111. The `.policy` sidecar
records when the body was fetched and its `max-age`. A fresh entry is
served directly; a stale one is revalidated with `If-None-Match`, and a
`304 Not Modified` slides the window forward without refetching the body.
Metadata and sdist records are immutable: cached once, never revalidated.
A 404 is remembered briefly so a repeated lookup of an absent name is
answered from cache, offline included.

## Parsed-listing accelerator

Turning a listing body into records means a JSON decode plus wheel and
sdist filename parsing, the bulk of a warm resolve's work. The
`simple-parsed-v0/` bucket stores those records so a warm hit rehydrates
them and never reads the large raw body.

Each parsed blob is bound to the exact body it came from by a `body_digest`
that the `.policy` also carries. On a hit the two digests are compared;
they match only while the body is unchanged, so any body update
invalidates the blob by construction and forces a rebuild from the raw
body. A blob is also rebuilt when it was written by a different nab build
or interpreter, or is corrupt. The raw body remains authoritative: the
accelerator is only ever a derived copy, and a rebuild is a reparse of
whatever body is on disk.

## Verifying and clearing

`nab cache verify` walks every bucket read-only and reports any entry that
will not parse, including a parsed blob that is not decodable. It checks
structure only, not freshness: a stale-but-valid parsed blob is not
corrupt, since the digest binding retires it at read time. `nab cache
clear` removes every bucket, returning the cache to cold. Both refuse a
root that does not look like a nab cache and never follow a symlink out of
it.
