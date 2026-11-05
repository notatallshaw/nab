# Platform tags

```toml
[tool.nab.matrix]
python = "==3.15"
platforms = [
    "windows_amd64",
    { id = "linux_x86_64", libc = "musl", runs-on-libc = "1.2" },
    { id = "macos_arm64", runs-on-macos = "14.0" },
]
```

A bare platform id uses its defaults. A table adds wheel compatibility settings; each table requires
`id`. See the
[tag specification](https://packaging.python.org/en/latest/specifications/platform-compatibility-tags/)
for filename formats.

## Supported platforms

| System  | IDs                                                           |
| ------- | ------------------------------------------------------------- |
| Linux   | `linux_x86_64`, `linux_aarch64`, `linux_i686`, `linux_armv7l` |
| macOS   | `macos_arm64`, `macos_x86_64`                                 |
| Windows | `windows_amd64`, `windows_arm64`                              |

## Linux libc

Within `[tool.nab.matrix]`:

```toml
platforms = [{ id = "linux_x86_64", libc = "glibc", runs-on-libc = "2.28" }]
```

This accepts `manylinux_2_17` and `manylinux_2_28` wheels and rejects `manylinux_2_34`. Use
`libc = "musl"` for musllinux wheels. One target accepts one libc family; glibc is the default.

With no `runs-on-libc`, Nab retains every libc level and leaves compatibility to installation. The
configured major must be glibc `2` or musl `1`. Raising the floor admits newer wheels without
excluding older compatible wheels. These settings apply only to Linux.

## macOS version

Within `[tool.nab.matrix]`:

```toml
platforms = [{ id = "macos_arm64", runs-on-macos = "14.0" }]
```

Wheels requiring macOS 15 are excluded. With no floor, all macOS levels remain eligible. The minimum
is 10.4 on x86-64 or 11.0 on Apple Silicon. `runs-on-macos` applies only to macOS.

## PyPy ABI

```toml
[tool.nab.matrix]
python = ">=3.10,<3.13"
platforms = ["windows_amd64", "macos_arm64"]

# Add PyPy with a specific native ABI.
[[tool.nab.matrix.include]]
python = "==3.11"
implementations = ["pypy"]
platforms = [
    { id = "windows_amd64", pypy-abi = "pp80" },
    { id = "macos_arm64", pypy-abi = "pp80" },
]
```

`pp80` accepts `pypy311_pp80` native wheels and excludes `pypy311_pp73`; pure Python wheels remain
eligible. Without `pypy-abi`, Nab retains all native ABIs for that Python minor. A host resolve uses
the running interpreter's ABI.

The setting also works in `environment.platform` or after a CLI platform ID as `pypy-abi=pp80`.
Every target using it must be PyPy. ABI variants cannot be separate targets for the same platform:
PEP 508 cannot distinguish them.

## Free-threaded CPython

For one declared target:

```toml
[tool.nab.environment]
python = "3.15"
platform = { id = "linux_x86_64", free-threaded = true }
```

`free-threaded = true` selects the `cp3XXt` ABI and excludes ordinary `cp3XX` and `abi3` wheels. It
requires CPython 3.13 or newer; the default is `false`. Runtime GIL settings do not change the ABI.
For both builds in one matrix, set
[matrix.free-threaded = [false, true]](target-matrix.md#select-python-builds).

## Kernel marker values

```toml
[tool.nab.matrix]
python = "==3.15"
platforms = [
    { id = "linux_x86_64", platform-release = "5.15.0", platform-version = "custom" },
]
```

`platform-release` and `platform-version` set PEP 508 markers without changing wheel tags. Both
default to `""`; comparisons against an empty value evaluate false. For example, a dependency
requiring `platform_release >= "5.10"` is dropped unless the target declares a matching kernel.

## Multiple variants and builds

For glibc and musl targets sharing a lock:

```toml
[tool.nab.matrix]
python = "==3.15"
platforms = [{ id = "linux_x86_64", libc = "glibc", runs-on-libc = "2.28" }]

# Add musl without repeating an ID within one platform list.
[[tool.nab.matrix.include]]
platforms = [{ id = "linux_x86_64", libc = "musl", runs-on-libc = "1.2" }]
```

Each entry may name a platform id once. PEP 508 cannot distinguish libc families or threading
variants, so targets sharing a marker environment must agree on package selections and pins. Nab
rejects incompatible selections.

Declared platform targets default to `build-policy = "never"`. Host metadata builds require both an
`assume-universal-deps` assertion and an explicit [build policy](build-policy.md).
