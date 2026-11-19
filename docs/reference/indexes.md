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

<a name="index-serialization"></a>

## Serialization

Nab negotiates JSON or HTML through the
[Simple repository API](https://packaging.python.org/en/latest/specifications/simple-repository-api/).
Set `serialization` to `"json"` or `"html"` to require one format.

nab requests and decodes the selected format. If the index returns another format or HTTP 406, nab
fails the resolve without trying the next index.

nab caches listings separately for each `serialization` setting.

`serialization` is not settable on a `file://` index, `"negotiate"` included. A local index is read
from disk with no `Accept` negotiation, so the key is rejected.

Nab reads Simple API version 1 pages. An unversioned page is treated as version 1.0. A newer minor
version produces a warning. A higher major version stops the lock, even if another configured index
has the package.

Nab checks HTML pages when it downloads them. Listings cached by older releases contain the file
records without the original HTML version tag. That tag can be checked the next time Nab downloads
the page.

### HTML limitations

Pinning `html` gives up the extra data PEP 700 defines for the JSON listing. Two of those losses
matter:

- HTML has no standard upload-time field. Nab reads the non-standard `data-upload-time` attribute on
  both HTTP and local HTML pages. A cutoff excludes remote files with no reported time.
  [Files on disk remain eligible](reproducibility.md#fixed-upload-cutoffs).
- A page publishes its hashes in the URL fragment. A file whose only fragment digest is md5 gives
  `nab lock` nothing to record, and the PEP 751 and requirements writers then fail on the missing
  hash.
