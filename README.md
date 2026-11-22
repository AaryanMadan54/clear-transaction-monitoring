# Clear: transaction monitoring review

Hackathon submission: a policy engine with guarded optional AI review through OpenRouter and a local analyst dashboard. Standard-library Python; no dependencies.

```powershell
python system/run institutions/bank output/bank.jsonl
python system/serve.py
```

For AI-assisted adjudication, set `OPENROUTER_API_KEY` in your terminal and run:

```powershell
python system/run institutions/bank output/bank.jsonl --mode hybrid
```

Default model: `google/gemini-2.5-flash-lite`. Default mode is rules. AI requires a funded OpenRouter account; HTTP 402 retains the baseline result. Credentials and supplied institution input data are excluded from this repository. Place the hackathon input folders under `institutions/` to run locally.

Download `submission.zip` for the packaged system and all four 1,000-case development outputs. See [system/README.md](system/README.md) for policy boundaries, run commands, validation and limitations. Development accuracy is unmeasured because no reference labels were supplied.

For an explicitly separate xAI development check with a local credential file, see [development/README.md](development/README.md). The shared review pipeline has live smoke tests; the judged runner continues to use OpenRouter only.

Validation: 87 synthetic tests; all 4,000 saved development outputs validated; 30 successful live development-provider reviews across the four institutions. These checks establish integration and policy boundaries, not classification accuracy. OpenRouter generation is still blocked by account HTTP 402.
