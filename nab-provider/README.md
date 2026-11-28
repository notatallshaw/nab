# nab-provider

Apply Python packaging rules during dependency resolution. This package supplies candidate versions
and their dependency constraints to [`nab-resolver`](https://pypi.org/project/nab-resolver/),
including environment markers and build, distribution, and VCS policies.

It performs no networking, file access, or subprocess calls.

## When to use it

For the command-line tool, install [`nab`](https://pypi.org/project/nab/).

Use `nab-provider` if you want nab to apply Python packaging rules and your application already
handles networking and caching. Implement `nab_provider.fetch_port.FetchPort` to supply package
listings and metadata. In the nab CLI, `nab-project` provides that implementation.
`nab_provider.testing` provides an in-memory version for tests.

If your installer already decides which files are eligible for the target Python and platform, you
can use `nab-candidates` directly. You supply those files and their dependencies. The library
chooses a compatible set and explains any yanked files it selects.

Use `nab_provider.ranges.VersionRange` for root constraints and as the resolver's `range_type`. Pass
`root_version="0"` and `format_range=provider.format_range` when constructing the resolver.

The API is experimental. Pin an exact version when embedding it.

## Wheel tags

`wheel_tag_set(filename)` caches repeated filename queries. `parse_wheel_tag_set(filename)` shares
parsing by tag suffix for bulk listings.

## Value records

Nab's record constructors declare their fields explicitly. `nab_provider.value` shares value
comparison and copying across the provider, index and project layers. Fixed fields use `Final`
annotations; assignment is not checked at runtime. `replace()` calls the concrete constructor,
including validation and derived fields:

```python
from nab_provider.tags import PlatformSpec
from nab_provider.value import replace

platform = PlatformSpec("linux_x86_64")
platform = replace(platform, runs_on_libc=(2, 28))
```
