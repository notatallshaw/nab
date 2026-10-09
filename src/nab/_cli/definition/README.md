# Constructing the CLI

Start in [`options.py`](options.py). It declares the named option tables in help order.

## From declarations to generated files

```mermaid
flowchart LR
    declarations[Declarations] --> records[Opt records]
    records --> files[Generated files]
```

`build.py` turns declarations into `Opt` records. `gen_cli.py` writes the parser tables,
configuration registry, and global-flags reference. `gen_bijection.py` writes typed calls that check
handler signatures.

## Where each rule lives

- [`options.py`](options.py): the shipped declarations.
- [`rows.py`](rows.py): typed constructors such as `Value`, `Tri`, `Many`, and `Layer`.
- [`tables.py`](tables.py): table defaults and duplicate checks.
- [`types.py`](types.py): scalar types, choices, and nullability.
- [`build.py`](build.py): combine row metadata and inferred types into `Opt`.
- [`model.py`](model.py): `Opt`, flag names, and option validation.

## Reading a declaration

```python
from nab._cli.definition.rows import Value
from nab._cli.definition.tables import Table


class Example(Table, on=("lock",)):
    max_concurrency = Value[int | None](help="maximum concurrent downloads")
```

The attribute names `--max-concurrency`; the type parameter declares integer conversion and accepts
`None`. `Table` supplies command membership, scope, and a documentation page; a row can override
`on=` and `docs=`.

`Layer[C]` adds configuration parsing, rendering, and its built-in default. That default differs
from the CLI default: an absent flag can pass `None` while the configuration ladder supplies a
value. `under=` rows set individual keys of a configuration table; `Key` declares a file-only
setting.

## Changing an option

Edit `options.py` and its handler signature, then run from the repository root:

```bash
python tasks/gen_cli.py --write
python tasks/gen_bijection.py --write
```

Commit the generated files. Both generators support `--check`. `test_option_rows.py` covers
construction rules; `test_cli_conformance.py` and the generated typed calls check handler
signatures. [Runtime parsing](../README.md) consumes `spec.py` without importing these declarations.
