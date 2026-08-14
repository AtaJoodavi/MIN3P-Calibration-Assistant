# V15.4 GPT Calibration Campaign Reviewer

V15.4 is a read-only reporting extension. It does not run MIN3P and does not modify `agent_config.xlsx`, V14 optimizer state, parameter bounds, or best-parameter files.

## New files

- `modules/campaign_review_V15_4.py`: deterministic campaign analysis, strict GPT review, deterministic fallback, JSON schema, and Markdown rendering.
- `generate_v15_campaign_analysis.py`: creates deterministic `campaign_analysis_V15_4.json` and `.xlsx`.
- `generate_v15_gpt_campaign_review.py`: creates the structured scientific review.
- `config/gpt_campaign_review_V15_4.yaml`: GPT settings.
- `tests/test_v15_4_campaign_review.py`: offline tests.
- `verify_v15_4_campaign_review.py`: smoke verifier.

The main `generate_v15_scientific_report.py` command now embeds the campaign review in the existing Markdown and HTML reports.

## Configure GPT

Edit:

```yaml
# config/gpt_campaign_review_V15_4.yaml
enabled: true
model: gpt-5.5
reasoning_effort: medium
max_output_tokens: 5000
verbosity: medium
```

Set the API key in PowerShell for the current terminal:

```powershell
$env:OPENAI_API_KEY="YOUR_API_KEY"
```

Do not save the API key in the YAML file or repository.

## Recommended command

```powershell
python .\generate_v15_scientific_report.py `
  --dat-file ..\01_input\HCT.dat `
  --conceptual-image-mode deterministic `
  --conceptual-detail paper `
  --campaign-review-mode auto
```

`auto` uses GPT when the YAML setting is enabled and `OPENAI_API_KEY` is available. Otherwise it writes a deterministic scientific review and records the fallback reason.

To require GPT and stop on an API/configuration error:

```powershell
python .\generate_v15_scientific_report.py `
  --dat-file ..\01_input\HCT.dat `
  --conceptual-image-mode deterministic `
  --conceptual-detail paper `
  --campaign-review-mode gpt
```

To create only the campaign evidence and review:

```powershell
python .\generate_v15_campaign_analysis.py
python .\generate_v15_gpt_campaign_review.py --mode auto
```

## New outputs

Under `..\05_reports\V15_scientific_report`:

- `campaign_analysis_V15_4.json`
- `campaign_analysis_V15_4.xlsx`
- `campaign_review_V15_4.json`
- `campaign_review_V15_4.md`
- `campaign_review_manifest_V15_4.json`
- updated `V15_calibration_story_report.md`
- updated `V15_calibration_story_report.html`

## Evidence boundary

Python calculates run counts, acceptance counts, objective statistics, closest candidates, species RMSE/bias, warning frequencies, charge balance, step-size counts, tested groups, and stagnation indicators. GPT interprets this JSON but cannot change the values. Structured Outputs and local validation constrain the response format.
