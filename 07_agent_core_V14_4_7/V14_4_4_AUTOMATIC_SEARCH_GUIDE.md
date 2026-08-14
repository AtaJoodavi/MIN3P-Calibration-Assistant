# V14.4.4 automatic range-adaptive initial search

V14.4.4 estimates the first calibration scale automatically from each parameter's existing `value`, `min`, and `max`. No `initial_search_mode`, `initial_factor`, or `initial_range_fraction` columns are required.

## Positive logarithmic parameters

For valid positive bounds:

```text
R = log10(max / min)
```

The initial transformed-range fraction grows smoothly from the global default (`0.05`) toward the scale needed for a one-decade (`x10`) probe over a six-decade range. The requested first factor is capped at `x10`.

With the default settings:

| Configured range | Typical first factor |
|---|---:|
| very narrow, ~x1.5 | ~x1.02 |
| 2 decades | ~x1.5 |
| 3 decades | ~x2.1 |
| 4 decades | ~x3.2 |
| 5 decades | ~x5.4 |
| 6+ decades | x10 maximum |

After the first direction family, the existing V14 shrink/refinement logic narrows the step automatically.

## Bounded-linear parameters

If the parameter is represented in bounded-linear space and has valid bounds, the default first move is:

```text
0.05 * (max - min)
```

Example:

```text
bottom_head = -0.25
min = -0.30
max = 0.00
first numeric change = 0.05 * 0.30 = 0.015
```

so the first candidates are approximately `-0.235` and `-0.265`.

## Relative parameters

When valid bounds exist, V14.4.4 converts the default range fraction to a local relative fraction so the first numeric change equals 5% of the configured span.

## Safe fallback

If range analysis cannot be performed, V14.4.4 does not fail. Examples include:

- logarithmic lower bound equal to zero;
- negative logarithmic bounds;
- missing min or max;
- `min == max`;
- non-finite values.

The controller then uses the global initial step, normally `0.05`. A positive logarithmic parameter without usable positive bounds therefore falls back to the legacy reciprocal local pair (`x1.05` and `/1.05`).

## Hard bounds

`min` and `max` remain hard safety limits. Even when the automatic first factor is `x10`, the actual candidate is clamped to the configured bound if necessary. The audit fields record the requested and applied factors.

## Migration

Already-tested parameters retain their current adaptive step. Only pristine/unexplored parameters are migrated to the V14.4.4 automatic initial scale.

## Preview

```powershell
python .\preview_v14_4_4_initial_scales.py --active-only --xlsx
```

This is read-only and does not run MIN3P or alter optimizer state.
