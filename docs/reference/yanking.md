# Yanked releases

A package publisher can mark distribution files as *yanked*: still available to download, but withdrawn from normal use. nab skips yanked files unless an exact version requirement allows them.

## Choosing a version

Suppose `example-lib` has versions `1.1` and `1.2`, and the publisher has yanked `1.2`.

| Requirement | What nab does |
| --- | --- |
| `example-lib>=1` | Skips `1.2` and can choose `1.1`. |
| `example-lib==1.2` | Can use the yanked `1.2`. |
| `example-lib==1.*` | Skips `1.2`; a wildcard does not allow yanked files. |
| `example-lib>=1.2,<=1.2` | Cannot use the yanked `1.2`; a range does not count as an exact pin. |

An exact `===` requirement also allows matching yanked files. Other requirements still have to be satisfied, including Python-version and platform compatibility.

If an exact pin matches both yanked and non-yanked files, nab first looks for a live alternative. It can change an independently requested package's version to make that alternative work. Explicit version requirements still apply.

A selected parent that pins its yanked dependency keeps its version preference. For example, nab can keep `app==2` requiring yanked `lib==2` even when older `app==1` would use live `lib==1`. During a live-alternative check, other selected live files stay live; nab does not simply exchange which package is yanked.

Missing offline metadata cannot prove that a live alternative fails. Nab can use a fully known alternative, or report that resolution is incomplete.

## When a dependency needs a yanked release

Suppose your project depends on `example-app`, which requires `example-lib==1.2`. That dependency's exact pin allows nab to use the yanked `example-lib` release. You do not need to repeat the pin in your project.

An exact pin in a constraint works too, provided the package is already required. Requirements for an unselected extra or a different Python environment do not allow yanked releases for your current environment.

## Understanding warnings

When a resolution uses a yanked file, nab reports the admitting input pin or dependency and the publisher's reason when available. Check that reason before deciding whether to keep the pin or choose another version.

Yanking applies to individual files. A release can have a yanked source archive and a non-yanked wheel, so the result can differ between platforms or when you request a source-only installation.
