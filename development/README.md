# Development checks

The submitted runner uses OpenRouter exclusively. The optional xAI harness exercises the same request schema, response parser, citation validation and hybrid policy guards using a separate development endpoint. It is excluded from `submission.zip` and never overwrites the four submitted output files.

Save `XAI_API_KEY=...` in a local `.env` file or set it in your terminal. Both `.env` and Notepad's `.env.txt` filename are ignored by Git. Never commit credentials.

```powershell
python development/xai_smoke.py --env-file .env.txt --institution bank --limit 6 --budget 0.50
python development/xai_smoke.py --env-file .env.txt --institution payments_fx --limit 6 --budget 0.50 --financial-evidence
```

The harness selects unresolved eligible cases from the supplied input; it contains no case-ID or answer lookup. It writes local audit files under `system/reports/`, which are excluded from Git. `changed_results` includes changed explanations and citations; `changed_verdicts` counts verdict changes. Neither measures accuracy because reference labels are absent.

Run the regression tests and validate the saved outputs:

```powershell
python -m unittest discover -s system -p "test*.py"
python system/verify.py --repeat 5 --sample 200
```

Rebuild the archive after verification:

```powershell
python development/package_submission.py
```

The packager checks an explicit file allowlist for credential strings and development endpoints, preserves the executable runner, and excludes local keys, raw case audits and this development directory.
