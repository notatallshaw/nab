# Platform tags

These settings control Nab's wheel filtering. The
[platform compatibility tags specification](https://packaging.python.org/en/latest/specifications/platform-compatibility-tags/)
defines the tag formats.

## Configure a platform

A `platforms` entry is either a bare platform id, which takes that platform's defaults, or a table
declaring wheel compatibility settings:

```toml
[tool.nab.matrix]
python = ">=3.11,<3.14"
platforms = [
    "windows_amd64",
    { id = "linux_x86_64", libc = "musl", runs-on-libc = "1.2" },
    { id = "macos_arm64", runs-on-macos = "14.0" },
    { id = "linux_aarch64", platform-release = "5.15.0" },
]
```

## Supported platforms

- Linux: `linux_x86_64`, `linux_aarch64`, `linux_i686`, `linux_armv7l`.
- macOS: `macos_arm64`, `macos_x86_64`.
- Windows: `windows_amd64`, `windows_arm64`.

Each platform table requires `id`. Use only settings for that platform; see the sections below.

## Linux libc

`libc` defaults to `"glibc"` for manylinux wheels. Select `"musl"` for musllinux wheels. A target
accepts one family.

Within `[tool.nab.matrix]`, set the oldest supported libc version:

```toml
platforms = [{ id = "linux_x86_64", libc = "glibc", runs-on-libc = "2.28" }]
```

Unset, `runs-on-libc` accepts wheels of any libc level and leaves compatibility to installation. The
configured major must be glibc `2` or musl `1`.

For a glibc 2.28 target:

- `manylinux_2_17` and `manylinux_2_28` wheels run there and are accepted.
- a `manylinux_2_34` wheel cannot run there and is dropped: it needs a newer glibc.

Raising `runs-on-libc` accepts wheels requiring newer libc; it does not exclude older compatible
wheels.

## macOS version

`runs-on-macos` is unset by default and accepts wheels of any macOS level. Within
`[tool.nab.matrix]`, declare the oldest supported macOS:

```toml
platforms = [{ id = "macos_arm64", runs-on-macos = "14.0" }]
```

With `runs-on-macos = "14.0"`, wheels requiring macOS 15 are excluded. A value below 10.4 on x86-64
or 11.0 on Apple Silicon is a config error.

## PyPy ABI

PyPy ABIs are independent of the Python language version. Leave `pypy-abi` unset to retain wheels
for any native PyPy ABI of the declared Python minor, deferring ABI compatibility to installation.
To target one ABI in a mixed matrix, declare the suffix in PyPy's platform override:

```toml
[tool.nab.matrix]
python = ">=3.10,<3.13"
platforms = ["windows_amd64", "macos_arm64"]
implementations = ["cpython", "pypy"]

[tool.nab.matrix.overrides.implementations.pypy]
python = "==3.11"
platforms = [
    { id = "windows_amd64", pypy-abi = "pp80" },
    { id = "macos_arm64", pypy-abi = "pp80" },
]
```

Here `pp80` selects `pypy311_pp80` native wheels and excludes `pypy311_pp73`; pure-Python wheels
remain eligible. The same setting works in `environment.platform` and as `pypy-abi=pp80` after a CLI
platform ID. An explicit ABI requires every target using that platform table to be PyPy. ABI
variants cannot be separate matrix targets for the same platform because PEP 508 markers cannot
distinguish them. A host resolve uses the running interpreter's actual ABI.

## Free-threaded CPython

`free-threaded` defaults to `false`. Setting it to `true` selects the `cp3XXt` ABI, excluding
ordinary `cp3XX` and `abi3` wheels. Every target using that platform setting must be CPython 3.13 or
newer. An older Python or another implementation is a config error in either a matrix or
`[tool.nab.environment]`.

```toml
[tool.nab]
build-policy = "never"

[tool.nab.environment]
python = "3.13"
platform = { id = "linux_x86_64", free-threaded = true }
```

## Kernel marker values

`platform-release` and `platform-version` set PEP 508 marker values and do not affect wheel-tag
selection. Both default to `""`; every comparison against an empty value evaluates False, so a
dependency selected by `platform_release >= "5.10"` (or on any other comparison) is dropped. A
target that does run that kernel has to say so.

## Matrix restrictions

nab rejects settings for the wrong platform: `libc` and `runs-on-libc` apply only to Linux, and
`runs-on-macos` applies only to macOS.

Each implementation's platform list may name an id once. PEP 508 cannot distinguish libc families or
free-threaded builds for the same interpreter, so duplicate ids would render the same lock marker.
Lock another libc family or ABI for that interpreter in a separate run and file.

For declared platform targets, including singleton matrices, `build-policy` defaults to `never`.
Host metadata builds require an `assume-universal-deps` assertion and an explicit build policy. See
[Build policy](build-policy.md).
