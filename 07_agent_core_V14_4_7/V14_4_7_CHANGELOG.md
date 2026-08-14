# V14.4.7 Changelog

V14.4.7 is a CLI/status/audit release on top of V14.4.6. It does not change
candidate generation, objective calculation, acceptance/rejection rules,
automatic range scaling, cache semantics, directional safety, reactivation,
or transaction recovery.

## Clear invocation limits

New options:

- `--max-candidates N`: process at most N optimizer candidate cycles. Cache hits
  count because they are still optimizer decisions.
- `--max-physical-runs N`: allow at most N new cache-miss MIN3P executions.
  Cache hits do not consume this quota.
- `--max-runs N`: retained as a backward-compatible alias for
  `--max-candidates N`.

If only `--max-physical-runs` is supplied, no candidate-cycle limit is injected;
the invocation continues through cache hits until the physical-run quota is
reached, the optimizer converges/blocks, or another safe-stop condition occurs.

## Explicit invocation summary

Every auto invocation now prints and persists:

- candidate cycles selected/completed;
- physical MIN3P runs;
- cache hits;
- bound/no-op skips;
- accepted/rejected/invalid candidates;
- best objective before/after;
- explicit stop reason and stop detail;
- current optimizer convergence status.

The summary is stored in `v14_optimizer_state.xlsx` under
`last_invocation_*` fields.

Stop reasons include:

- `max_candidates_reached`
- `max_physical_runs_reached`
- `true_convergence`
- `temporarily_blocked`
- `safe_stop_requested`
- `manual_stop_file`
- `optimizer_stop_requested`
- `transaction_guard_stop`
- `user_interrupt`
- `unexpected_failure`

## Examples

Exactly the old behavior, but with clearer naming:

```powershell
python .\min3p_ai_pipeline_V14.py --mode auto --max-candidates 20 --max-changes 1
```

Allow up to 20 actual new MIN3P simulations, regardless of intervening cache hits:

```powershell
python .\min3p_ai_pipeline_V14.py --mode auto --max-physical-runs 20 --max-changes 1
```

Use both limits and stop when either is reached:

```powershell
python .\min3p_ai_pipeline_V14.py --mode auto --max-candidates 50 --max-physical-runs 20 --max-changes 1
```
