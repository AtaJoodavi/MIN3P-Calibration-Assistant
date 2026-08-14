# V14.4.5 Changelog

V14.4.5 adds pre-MIN3P bound/no-op candidate detection on top of V14.4.4.

- Replaces exact `new_value == old_value` checks with a conservative scaled tolerance.
- Logs `candidate_blocked_by_bound_noop` in `v14_optimizer_events.xlsx`.
- Does not start MIN3P or increment physical valid-run count for blocked no-op candidates.
- Immediately continues with the remaining legal direction.
- Applies the same no-op assessment to interaction-pair moves.
- Leaves automatic range-adaptive initial scaling, objective rules, acceptance, rollback, and cache logic unchanged.
