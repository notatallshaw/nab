# Platform tags

These settings control Nab's wheel filtering. The
[platform compatibility tags specification](https://packaging.python.org/en/latest/specifications/platform-compatibility-tags/)
defines the tag formats.

A `platforms` entry is either a bare platform id, which takes that platform's defaults, or a table
declaring the wheel-tag knobs:

```toml
[tool.nab]
mode = "universal"

[tool.nab.matrix]
python = ">=3.11,<3.14"
platforms = [
    "windows_amd64",
    { id = "linux_x86_64", libc = "musl", runs-on-libc = "1.2" },
    { id = "macos_arm64", runs-on-macos = "14.0" },
    { id = "linux_aarch64", platform-release = "5.15.0" },
]
```

| Key                | Default                     | Meaning                                                                                                                           |
| ------------------ | --------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `id`               | required                    | `linux_x86_64`, `linux_aarch64`, `linux_i686`, `linux_armv7l`, `macos_arm64`, `macos_x86_64`, `windows_amd64`, or `windows_arm64` |
| `libc`             | `"glibc"`                   | The Linux C library: `"glibc"` or `"musl"`                                                                                        |
| `runs-on-libc`     | unset (accept any level)    | The glibc/musl the lock must run on; wheels needing newer are dropped                                                             |
| `pypy-abi`         | unset (accept any PyPy ABI) | Exact PyPy ABI suffix, such as `"pp80"`; requires a PyPy target                                                                   |
| `runs-on-macos`    | unset (accept any level)    | The macOS the lock must run on; wheels needing newer are dropped                                                                  |
| `platform-release` | `""`                        | The `platform_release` marker value                                                                                               |
| `platform-version` | `""`                        | The `platform_version` marker value                                                                                               |
| `free-threaded`    | `false`                     | Target the free-threaded (`cp3XXt`) CPython build                                                                                 |

A machine links one C library, so a target accepts one family's wheels: a `glibc` target takes
manylinux wheels and never musllinux ones, and a `musl` target the reverse.

Left unset, `runs-on-libc` accepts wheels of any manylinux or musllinux level and leaves
compatibility to install time. Set it to the oldest glibc or musl the lock must support. Its major
must be glibc `2` or musl `1`.

`runs-on-libc = "2.28"` means the lock must run on glibc 2.28. A wheel is lockable only if it runs
on every target machine, so:

- `manylinux_2_17` and `manylinux_2_28` wheels run there and are accepted.
- a `manylinux_2_34` wheel cannot run there and is dropped: it needs a newer glibc.

Older wheels are never excluded; the knob only rules out wheels that need something newer than the
declared system, so a higher `runs-on-libc` accepts more wheels, not fewer. Newer systems are always
fine: a lock that runs on glibc 2.28 runs on anything newer (the same holds for macOS).

`runs-on-macos = "14.0"` sets the minimum machine to macOS 14. It accepts `macosx_10_9` and
`macosx_14_0` wheels but drops `macosx_15_0`. A value below the architecture's oldest macOS, 10.4 on
x86_64 or 11.0 on Apple Silicon, is a config error.

PyPy ABIs are independent of the Python language version. Leave `pypy-abi` unset to retain wheels
for any native PyPy ABI of the declared Python minor, deferring ABI compatibility to installation.
To target one ABI in a mixed matrix, declare the suffix in PyPy's platform override:

```toml
[tool.nab]
mode = "universal"

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
remain eligible. The same knob works in `environment.platform` and as `pypy-abi=pp80` after a CLI
platform ID. An explicit ABI requires every target using that platform table to be PyPy. ABI
variants cannot be separate matrix targets for the same platform because PEP 508 markers cannot
distinguish them. A host resolve uses the running interpreter's actual ABI.

A knob belongs to its platform. `libc` and `runs-on-libc` are Linux knobs and `runs-on-macos` is a
macOS one, so declaring one on a platform that cannot read it is a config error. It would select no
wheel, and it would still record the machine the lock was resolved for.

`platform-release` and `platform-version` are the exception: they set PEP 508 marker values and
never enter wheel-tag selection. Left empty, every comparison against them evaluates False, so a
dependency selected by `platform_release >= "5.10"` (or on any other comparison) is dropped. A
target that does run that kernel has to say so.

`free-threaded` selects the `cp3XXt` ABI, excluding ordinary `cp3XX` and `abi3` wheels. Every target
using that platform setting must be CPython 3.13 or newer. An older Python or another implementation
is a config error in either a matrix or `[tool.nab.environment]`.

Each implementation's platform list may name an id once. PEP 508 cannot distinguish libc families or
free-threaded builds for the same interpreter, so duplicate ids would render the same lock marker.
Lock another libc family or ABI for that interpreter in a separate run and file.

Each target impersonates a platform, so universal mode cannot build on the host: `build-policy`
defaults to `never` and cannot be raised. An explicit non-`never` value (global or in any override)
is a config error. See [Build policy](build-policy.md).
