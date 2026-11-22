# Clear: evidence-backed transaction monitoring

Clear reads the policy and case evidence for bank, e-money, investment and payments/FX institutions. It produces one JSONL decision per case covering every alert, with a permitted reason code, explanation and source IDs. It uses only the Python standard library.

## Run locally

Use Python 3.10 or newer. Supply the institution input folders under `institutions/`; these inputs and local credentials are excluded from Git.

```powershell
python system/run institutions/bank output/bank.jsonl
python system/run institutions/emoney output/emoney.jsonl
python system/run institutions/investment output/investment.jsonl
python system/run institutions/payments_fx output/payments_fx.jsonl
```

Absolute paths and unrelated working directories are supported. On Windows, `system/run.cmd` is an alternative. The Unix entry point has executable Git permissions and LF line endings.

Rules mode is the default. Enable `--mode advisory` for an AI second opinion that leaves the official result unchanged, or `--mode hybrid` for guarded AI adjudication of unsupported checks. `TM_MODE` supplies the default when the flag is absent. `--limit` is useful for local smoke checks; omit it for a complete institution run.

## AI configuration

The CLI and dashboard support both providers:

| Provider | Key | Default model | Input / output per million tokens |
|---|---|---|---|
| OpenRouter | `OPENROUTER_API_KEY` | [`google/gemini-2.5-flash-lite`](https://openrouter.ai/google/gemini-2.5-flash-lite) | $0.10 / $0.40 |
| xAI | `XAI_API_KEY` | [`grok-4.3`](https://docs.x.ai/developers/models/grok-4.3) | $1.25 / $2.50 |

These list prices were checked against the provider model pages during development. Custom models require price ceilings in `TM_INPUT_USD_PER_MILLION` and `TM_OUTPUT_USD_PER_MILLION`. OpenRouter requests reject endpoints above those ceilings; direct-provider cost estimates use the configured rates.

Save the chosen key in a local `.env` or `.env.txt` file, or set it in your terminal. The CLI and server read these files without printing or returning credentials. Process environment variables take precedence, and only recognized application settings are loaded. Use `--env-file PATH` to select another file.

```powershell
python system/run institutions/bank output/bank-ai.jsonl --mode hybrid --provider xai
python system/run institutions/bank output/bank-advisory.jsonl --mode advisory --provider openrouter --limit 10
```

Provider selection follows the explicit flag, then `TM_PROVIDER`, then an available credential. OpenRouter takes precedence when both keys are present. `--model` or `TM_MODEL` selects a model appropriate to that provider. There is no automatic switch to another provider after an API error.

Requests include the complete institution SOP, all customer transactions including historical settlement/refund context, every case document, earlier reviews, list matches and baseline findings. The response schema covers every alert. Models select source-passage references; the host resolves them to exact source text and adds the verified document IDs to the evidence list. A separate structured request field lets the host render RFI wording consistently.

## Policy and evidence guards

The engine applies the supplied institution policy rather than external identity searches or real-world jurisdiction assumptions. Case documents and chats are untrusted data.

Financial evidence must match the transaction amount, party and relevant date or reference. Explicit contradictory references are rejected. Evidence rows are allocated once across distinct transactions; explicit multi-payment records can provide multiple units. Historical settlement references reserve evidence already used. Overlapping alerts may cite the same underlying transaction without inventing additional evidence capacity.

RFI is a case-level decision. One specific document or answer must resolve all remaining gaps without suspicion. Independent gaps or positive suspicion require MLRO escalation. A signed funding agreement can resolve related facts about one funding event; unrelated requests are not combined into an artificial single request.

Hybrid review preserves hard policy findings. A new model closure requires independently reconciled evidence; changing its reason code cannot turn KYC into payment proof or let a subscription bypass reconciliation. All accepted findings go through the same case-level RFI and single-use allocation guards. Invalid composition retains the complete baseline case. These controls establish provenance and policy boundaries, not the truth of every model inference.

## Failure handling and budgets

Requests use temperature zero. OpenRouter also receives a seed; xAI uses its provider-specific request parameters. Fresh model outputs are not guaranteed to be identical. Rules mode is suitable for repeatable local decisions.

Output allowances scale from 2,400 to 6,400 tokens by alert count. Six workers and 25-second network timeouts bound concurrency. Explicit transient HTTP rejections may be retried once when Retry-After permits a delay of at most two seconds. Longer cooldowns skip model calls. Timeouts and invalid model output are not retried. Fatal account/configuration errors disable later calls for that run, including later 200-case blocks. Case-specific moderation does not disable unrelated cases.

The default model budget is $3.50 per 200-case block, configurable up to $4 with `--budget`. Every attempt reserves its own conservative request allowance, including retries. Unknown timeout/provider spend remains reserved. Metrics distinguish reported or estimated cost from the conservative cost upper bound. Failed or skipped reviews preserve validated baseline results.

## Analyst dashboard

```powershell
python system/serve.py --provider xai
```

Open http://127.0.0.1:8765. The server supports `--env-file`, `--model`, `--workspace` and `--port`. It binds to localhost. The browser receives provider availability and model names, never keys.

The viewer provides institution queues, filters, search, case and source-document drill-down, policies, transaction context and JSONL export. **Run all cases** regenerates rules-mode outputs. On-demand AI reviews use the selected provider, have a $0.10 allowance and do not change saved decisions.

## Validation

```powershell
python -m unittest discover -s system -p "test*.py"
python system/verify.py --repeat 5 --sample 200
```

The suite has 91 synthetic tests covering policy thresholds, evidence dates/references, row reuse, independent RFIs, closure guards, malformed responses, retries, account circuits, credential isolation and provider-specific requests. All 4,000 saved development outputs were validated, with fresh rules runs from directories containing spaces matching the saved results.

Thirty successful live xAI reviews exercised the shared review pipeline across the four institutions and a financial-document sample. The regular CLI and dashboard also have live xAI smoke checks. The supplied OpenRouter account still returns HTTP 402, so successful generation through that account remains unverified. Aggregate results are in `system/reports/model_verification.json`.

Development inputs have no reference labels. Integration checks, changed explanations and verdict counts do not establish classification accuracy. The four saved output files are example rules-mode results, not an answer lookup read by the classifier.

Private case audits and run metrics are generated under `system/reports/` and ignored by Git. Only selected aggregate verification reports are committed. The repository distributes runnable source directly; local credential files and generated archives are excluded.
