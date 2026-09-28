# Yanked releases

Yanked files have been withdrawn by their publisher. nab normally skips them, but an exact pin can allow them.

Suppose `lib==1.2` is yanked and `lib==1.1` is not:

| Requirement | Result |
| --- | --- |
| `lib>=1` | Uses `1.1`. |
| `lib==1.2` or `lib===1.2` | Can use the yanked `1.2`. |
| `lib==1.*` | Skips `1.2`; a wildcard is not an exact pin. |
| `lib>=1.2,<=1.2` | Fails; a range is not an exact pin. |

The pin can come from your requirements, a selected dependency, or a constraint on a required package. Pins for inactive extras or other environments do not apply.

## Dependencies and alternatives

If `app==2` requires yanked `lib==2`, nab can keep that combination even when older `app==1` would use non-yanked `lib==1`. You do not need to repeat the dependency's pin yourself.

For independently requested packages, nab prefers a working non-yanked file. For example, you request `lib==1` and `tools`. If the non-yanked `lib` file needs `tools==1`, nab prefers that over `tools==2` with a yanked `lib` file. Explicit pins still apply.

If matching non-yanked files cannot satisfy the dependencies, nab may use an allowed yank. Missing offline metadata does not establish that failure.

A warning shows which requirement allowed the yank and the publisher's reason, when provided. Yanking applies to individual files, so results can differ by platform or between wheels and source archives.
