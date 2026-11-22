# Development checks

The regular CLI and dashboard both support OpenRouter and xAI. The smoke harness exercises the same request schema, response parser, citation validation and hybrid policy guards on a bounded sample. It never overwrites the four saved development output files.

Save `XAI_API_KEY=...` in a local `.env` file or set it in your terminal. Both `.env` and Notepad's `.env.txt` filename are ignored by Git.

```powershell
python development/xai_smoke.py --env-file .env.txt --institution bank --limit 6 --budget 0.50
python development/xai_smoke.py --env-file .env.txt --institution payments_fx --limit 6 --budget 0.50 --financial-evidence
```

The harness selects unresolved eligible cases from the supplied input. It contains no case-ID or answer lookup. Private audit files stay under `system/reports/` and are excluded from Git. `changed_results` includes changed explanations and citations; `changed_verdicts` counts verdict changes. Neither measures accuracy without reference labels.

```powershell
python -m unittest discover -s system -p "test*.py"
python system/verify.py --repeat 5 --sample 200
```

The repository is distributed as runnable source. Credentials, input data and generated archives are excluded.
