# nab-provider

The IO-free half of [`nab`](https://pypi.org/project/nab/): the value types,
policies, and provider vocabulary that a resolve needs but that never touch a
socket, a file, a subprocess, or the clock.

It provides:

- The records a listing is made of, and the errors a fetch can report.
- The Simple-API serialization vocabulary and the source-subdirectory
  containment rule.

## When to use it

Use `nab-provider` if you are embedding nab's resolution logic in a host that
already owns its own networking and caching, and you want the resolver without
[`nab-index`](https://pypi.org/project/nab-index/) in the import graph. Most
users want [`nab`](https://pypi.org/project/nab/) instead.

The API is currently under rapid experimentation, if you use it
pin to an exact version.
