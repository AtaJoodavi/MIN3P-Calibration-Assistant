# V14.4.6 Changelog

V14.4.6 is a robustness-only release on top of V14.4.5. It does not change
candidate selection, objective calculation, automatic range scaling, bound/no-op
detection, acceptance thresholds, or transaction rollback rules.

## Changes

- Hardened shared V14 JSON/Excel state persistence against brief Windows locks.
- Atomic replacement now retries transient WinError 5/32/33 and `PermissionError`.
- Uses a unique temporary filename for every state write, avoiding collisions with
  stale or concurrent temporary names.
- Cleans the temporary file created by the current write attempt in `finally`.
- V14 best/config snapshot promotion now uses the same retrying atomic-copy path.
- Pipeline audit Excel writes use the shared retrying atomic writer.
- Cache-hit observation failures now say "while processing a cached candidate
  result" rather than incorrectly claiming a new MIN3P run completed.
- Suppressed the pandas all-NA concatenation `FutureWarning` only around flexible
  V14 audit/state table appends; data semantics are unchanged.

## Motivation

An HCT3 long campaign stopped safely after Windows returned WinError 5 while
promoting `v14_step_size_state.json.tmp` to `v14_step_size_state.json`. The file
was healthy immediately afterward, no other Python process was running, and the
transaction guard restored a clean optimizer checkpoint. V14.4.6 makes such
brief handle-release delays recoverable without stopping the campaign on the
first failed `os.replace` call.
