# V14.4 Evidence-Adaptive Calibration Development Plan

## V14.4.0 — implemented

**Range-normalized parameter stepping**

- logarithmic steps use a fraction of the configured log10 range;
- bounded-linear parameters use a fraction of the linear range;
- safe fallback remains for unusable bounds;
- audit metadata records how each candidate was generated.

## V14.4.1 — implemented

**Candidate cache and audit cleanup**

- exact evaluation-context cache prevents duplicate MIN3P runs;
- duplicate optimizer-event writes removed;
- step metadata propagated into the main decision log.

## V14.4.2 — implemented

**Protected local refinement**

- a parameter that first improves and then becomes bracketed earns one immediate reduced-step refinement pair;
- unfinished direction families still have highest safety priority;
- broader stage exploration resumes after the protected refinement pair unless a new improvement earns another round.

## V14.4.3 — superseded by V14.4.4

**Manual parameter-specific initial search scales**

This release introduced optional manual `initial_factor` / range-fraction settings. V14.4.4 removes the need for those columns by estimating the starting scale automatically from `value/min/max`.

## V14.4.4 — implemented

**Automatic range-adaptive initial search**

- positive logarithmic ranges infer a smooth first factor from `log10(max/min)`;
- very wide ranges are capped at `x10 / divide-by-10` by default;
- narrow ranges receive small first moves automatically;
- bounded-linear and relative parameters use their configured span;
- zero, negative, missing, equal, or invalid bounds use the safe global fallback;
- already-tested parameters keep their adaptive state during migration.

## Recommended next development

**Directional response memory from every valid trial**

Record accepted and rejected candidate response, including objective change per normalized step, directional slope, response magnitude, best known direction, and species-response information. Rejected runs should contribute sensitivity information instead of being used only to shrink the step.

After that, add **safe bracketed/parabolic 1-D refinement** and **time-series residual fingerprints** before considering surrogate-assisted optimization.
