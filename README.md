# Clear: evidence-backed transaction monitoring

A standard-library Python app for reviewing bank, e-money, investment and payments/FX cases, with a local analyst dashboard and guarded AI review using **OpenRouter or xAI**.

Clone the source and use Python 3.10 or newer. No packages, database or build step are required. The institution input data is excluded from Git; place the supplied folders under `institutions/bank`, `institutions/emoney`, `institutions/investment` and `institutions/payments_fx`.

```powershell
python system/run institutions/bank output/bank.jsonl
python system/serve.py
```

Open the dashboard at http://127.0.0.1:8765. Rules mode is the default and makes no model calls.

For AI-assisted review with xAI, put `XAI_API_KEY=your-key` in a local `.env` or `.env.txt` file, or set it in your terminal. These files are ignored by Git. Then run:

```powershell
python system/run institutions/bank output/bank-ai.jsonl --mode hybrid --provider xai
python system/serve.py --provider xai
```

For OpenRouter, set `OPENROUTER_API_KEY` and use `--provider openrouter`. The default models are `grok-4.3` for xAI and `google/gemini-2.5-flash-lite` for OpenRouter. An explicit `--env-file` can select another local credential file. Process environment variables take precedence. There is no automatic provider fallback after an API error.

Run the tests:

```powershell
python -m unittest discover -s system -p "test*.py"
```

Validation includes 91 synthetic tests, all 4,000 saved development outputs and 30 successful live reviews through xAI. Smoke tests establish integration and policy boundaries; reference labels are absent, so classification accuracy remains unmeasured. The supplied OpenRouter account still returns HTTP 402; failures retain baseline decisions.

See [system/README.md](system/README.md) for policy guards, budgets, audit records and verification, and [development/README.md](development/README.md) for additional smoke checks. This repository contains the runnable source directly.
