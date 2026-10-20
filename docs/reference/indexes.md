# Indexes

`[[tool.nab.indexes]]` declares the ordered list of named package indexes. Order is significant: nab
consults them left to right and takes the *first* one that lists a given package (presence-based,
matching uv's `--index-strategy first-index` default).

```toml
[[tool.nab.indexes]]
name = "pypi"
url  = "https://pypi.org/simple/"

[[tool.nab.indexes]]
name = "torch-cpu"
url  = "https://download.pytorch.org/whl/cpu"

[[tool.nab.indexes]]
name = "internal"
url  = "https://artifactory.example.com/api/pypi/pypi/simple/"
# Which Simple-API serialization to ask this index for.
serialization = "html"   # "negotiate" (default) | "json" | "html"
```

When `[[tool.nab.indexes]]` is omitted entirely, nab defaults to a single PyPI entry.

(index-serialization)=

## Serialization

Nab negotiates JSON or HTML through the
[Simple repository API](https://packaging.python.org/en/latest/specifications/simple-repository-api/).
Set `serialization` to `"json"` or `"html"` to require one format.

The pin covers the `Accept` header and decoder together. The wrong serialization, including a strict
406 response, is an error. The resolve ends; nab does not try the next index.

Listings fetched under one setting are not reused under another: a pinned index keeps its own
listing cache.

`serialization` is not settable on a `file://` index, `"negotiate"` included. A local index is read
from disk with no `Accept` negotiation, so the key is rejected.

### HTML limitations

Pinning `html` gives up the extra data PEP 700 defines for the JSON listing. Two of those losses
matter:

- A PEP 503 page carries no upload time. Unless the index emits the non-standard `data-upload-time`
  attribute, every file it serves is excluded once `uploaded-prior-to` applies.
- A page publishes its hashes in the URL fragment. A file whose only fragment digest is md5 gives
  `nab lock` nothing to record, and the PEP 751 and requirements writers then fail on the missing
  hash.
