# V14.4.7 sensitivity-first patch

This patch aligns the 1-D calibration workflow with the TP2 2-D campaign strategy.

## Changes

- When at least five parameters are active, test every active parameter once before ordinary calibration.
- Initial screening candidates are diagnostic only; they do not modify the accepted state.
- Screening responses are therefore measured against the same accepted baseline.
- Rank parameters by absolute objective response normalized by the fractional perturbation.
- Start ordinary calibration with the most sensitive finite parameter after screening.
- Retain the learned sensitivity as a deterministic ranking signal.
- GPT/ChatGPT supervisory code is retained internally but is inactive by default.
- GPT command-line switches are removed from the normal CLI so publication-facing commands remain deterministic.

## Default calibration command

```powershell
python .\min3p_ai_pipeline_V14.py --mode auto --max-physical-runs 20 --max-changes 1
```
