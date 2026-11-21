# Clear: evidence-backed transaction-monitoring review

A standard-library Python 3 system for bank, e-money, investment and payments/FX cases. It produces one decision per case, all alert IDs, a permitted reason code, a case-specific explanation and validated evidence IDs. No packages, database, external identity searches or training-case lookup tables are required.

## Run the submission

From the hackathon directory on Windows:

```powershell
.\system\run.cmd institutions\bank output\bank.jsonl
.\system\run.cmd institutions\emoney output\emoney.jsonl
.\system\run.cmd institutions\investment output\investment.jsonl
.\system\run.cmd institutions\payments_fx output\payments_fx.jsonl
```

On Linux/macOS, `run` has a Python shebang (the submission ZIP preserves executable permissions):

```sh
chmod +x system/run
system/run /absolute/path/institutions/bank /absolute/path/output/bank.jsonl
```

`python system/run <institution folder> <output file>` works on Windows, and `python3 system/run ...` works on POSIX. Full input/output paths work when the working directory is `system/` or any unrelated directory. The institution folder must have its supplied name. The executable only reads the supplied institution's data and writes the requested output and runtime artifacts inside `system/`.

## How decisions are made

1. Index all JSONL files, read `POLICY.md`, and join each case to every alert, its customer, transactions, evidence documents, previous reviews and referenced list entries. Markdown fields and tables are parsed directly. Evidence paths cannot escape the supplied folder.
2. Check each alert independently. Match document amounts, account details, counterparties, dated rows, business purpose and ownership. Financial evidence must also link to the payment date or an identifying transaction reference. A conflicting invoice/order reference or explicit due date defeats a superficial amount-and-supplier match. Ordinary historical bills and small funding within the declared profile are separated from the unusual flow, rather than allowing background activity to conceal an unrelated unexplained transfer.
3. Detect positive grounds for suspicion, including scam reports, explicit payment-splitting enquiries, conflicting source explanations, third-party investment or money-forwarding admissions, and listed-party ownership at the actual payment date.
4. Apply policy boundaries. Own-account ownership requires documentary proof; own-account activity at or above five times the declared baseline requires an updated-profile RFI. A payments/FX held payment at or above the policy invoice threshold requires a matching invoice/contract unless documentary proof establishes an own-account recipient. A name or address match alone never establishes a listed-party identity. Historical reviews are context, not automatic clearance of new activity.
5. Allocate financial evidence to distinct transactions using deterministic maximum matching: one takings/schedule/invoice row can cover one transaction, unless an explicit count and per-payment amount establish greater capacity. A single invoice cannot be reused for several identical payments. Prior payments with a demonstrably matching invoice reference reserve that evidence. Repeated references to the same actual transaction across overlapping alerts consume only one unit; ownership records remain reusable as identity facts. Booking confirmations provide one contribution slot per named traveller and a separate total-payment slot; a cancellation confirmation explicitly covers the subscription and its return.
6. Aggregate all findings with fixed ordering: ESCALATE before RFI before CLOSE. A case closes only when every alert, financial allocation and applicable held-payment check has passed. RFI is also checked at case level: one specific document or answer must resolve all outstanding requirements. One gift agreement can resolve the source and relationship of the same funding event, and one order contract can resolve linked funding and payouts. Independent unresolved funding and beneficiary gaps without a shared evidential link are referred to the MLRO; unrelated requests are not concatenated into an RFI. Positive suspicion takes precedence. Ties use policy authority, a fixed reason-code order and check ID.
7. Generate a short explanation and sorted evidence IDs. Validate complete alert coverage, allowed verdict/code pairings, existing case-related citations and a specific customer request for every RFI. Serialize sorted JSON keys and stable case order. An unsuccessful run does not silently replace an existing output with a partial submission.

Date linkage uses conservative reconciliation heuristics, not new institution policy: without an identifying reference or explicit payment terms, a financial document must be dated within 31 days before or seven days after the transaction. A row's dated entry, a due/payment date, or stated net-day terms takes precedence. Older invoices may still qualify through an explicit matching reference or current due date, provided the evidence has not already been allocated to an earlier settlement. Failing this check means the document does not establish closure; age alone is not evidence of crime. These checks are rerun after optional model adjudication, so model prose cannot bypass document dating or evidence capacity.

The engine is an interpretable prototype, not a claim of production AML assurance. Evidence extraction uses general document fields, relationships and patterns; unfamiliar wording can leave a case unresolved. Reference labels were not supplied, so overall accuracy and wrongful-close rates have not been measured.

## Optional model analysis

The default mode is **rules**, including when an OpenRouter key exists. This makes the supplied development outputs reproducible with the judges' key present. A model is explicitly enabled using `--mode` (or `TM_MODE`):

- `rules`: local policy/evidence checks only; no network calls or model charges.
- `advisory`: model second opinion recorded in the audit trail; the official decision stays with the local engine.
- `hybrid`: model findings can resolve unsupported checks or identify additional concern. Hard checks for policy requirements and positive suspicion remain authoritative.

The model integration uses **`google/gemini-2.5-flash-lite`**, exclusively through `https://openrouter.ai/api/v1/chat/completions`, with `OPENROUTER_API_KEY` read from the environment. Credentials are never stored in the source or sent to the browser. `--model`/`TM_MODEL` can select another OpenRouter model. The current default-model list prices are $0.10/million input tokens and $0.40/million output tokens, verified on 21 November 2026: [OpenRouter model page](https://openrouter.ai/google/gemini-2.5-flash-lite). Custom models require correct rate ceilings in `TM_INPUT_USD_PER_MILLION` and `TM_OUTPUT_USD_PER_MILLION`; provider routing rejects higher listed prices.

With an OpenRouter key already set in the terminal environment:

```powershell
python system/run institutions/bank output/bank.jsonl --mode advisory --limit 200
python system/run institutions/bank output/bank.jsonl --mode hybrid
```

`--limit` is for development benchmarks; do not use it to generate the final submission. Regenerate **all four** full output files in the selected final mode before submission. Do not change modes between generated outputs and judging. A model mode without a key exits with a clear error instead of silently changing behaviour.

Each request includes the complete institution SOP, relevant transactions, every case document, earlier reviews, list matches, and baseline findings. It requests structured findings for every alert, allowed reason codes, real evidence IDs and exact documentary quotes. The response validator rejects incomplete alert coverage, invented IDs/quotes, code/verdict mismatches and closures without documentary quotes. Documents are untrusted input and may not instruct the system. These checks verify structure and provenance, not the truth of every model inference.

Requests use temperature zero, a fixed seed, no automatic model/provider fallback, no retries, a 3,200-token output ceiling, six workers and 25-second network timeouts. Model modes have a conservative spend ceiling of $3.50 per 200-case block (configurable up to $4). Input reservations use UTF-8 byte counts as a conservative token upper bound. If the budget is exhausted or the API fails, the validated baseline remains in place and the audit records why. There is no answer cache or memorised development verdict store.

Temperature zero and a seed do **not** guarantee identical fresh model outputs. The default rules mode and advisory verdicts avoid that issue; hybrid mode needs repeat-run evaluation before being chosen for judging. A live 10-case hybrid smoke test on 21 November 2026 made three OpenRouter requests; all returned HTTP 402 (the supplied key account had no purchased credits). The ten baseline results were retained. Transport, structured requests, validation, budget, missing-key and fallback paths are tested using synthetic mocked responses. Successful live hybrid generation, cost, latency and accuracy remain unverified. The API key must belong to a funded OpenRouter account; application changes cannot resolve HTTP 402.

## Local analyst dashboard

```powershell
python system/serve.py
```

Open [the local dashboard](http://127.0.0.1:8765). An alternate port is supported with `--port 8766`. The server binds to loopback only and runs without dependencies.

The viewer includes institution queues, decision filters, name/case/rule search, pagination, per-alert findings, source-document and transaction drill-down, previous-review context, institution policies and JSONL export. **Run all cases** regenerates the four rules-mode outputs. AI second review is enabled only when the server has an OpenRouter key; its opinion does not change a saved result. An on-demand review has a separate $0.10 reservation ceiling and one request.

This viewer is optional presentation tooling. The judges' `run` command does not start or depend on it. The viewer uses only locally supplied synthetic data and no external assets or search services.

## Verification and performance

```powershell
python -m unittest discover -s system -p "test*.py" -v
python system/verify.py --repeat 5 --sample 200
```

Tests use synthetic fixtures, not development answers. They cover exact threshold boundaries, missing/wrong ownership proof, joint accounts, multiple-alert severity, dated listed-party control, name-only matches, invoice beneficiary/date/reference mismatches, duplicate cash/schedule rows, explicit multi-payment capacity, overlapping-alert reuse, a previously settled invoice, compatible and independent RFI requirements, suspicious splitting, citation validity, model validation and baseline fallback.

`verify.py` validates every saved development output and then runs fresh processes from a directory containing spaces with different hash seeds. It compares the full output bytes, including reason and evidence, across five 200-case runs for each institution and checks that they match the corresponding saved results. The report is written to `system/reports/verification.json`.

All 4,000 development results are checked for complete-case, alert-coverage, verdict/code and citation validation. The suite now contains 64 synthetic tests. `system/reports/verification.json` records the fresh-process reproducibility checks for the current generated outputs; regenerate it after decision changes. These runs use unrelated working directories containing spaces and different Python hash seeds, and compare full output bytes including explanations and evidence.

Measured rules-mode performance on this machine, including loading the supplied full institution files:

| Institution | Median seconds / 200 cases | Range over five fresh runs | Model cost |
|---|---:|---:|---:|
| Bank | 3.332 | 3.106–3.422 | $0 |
| E-money | 3.288 | 3.089–3.358 | $0 |
| Investment | 1.512 | 1.489–2.210 | $0 |
| Payments and FX | 3.839 | 3.000–4.257 | $0 |

The final full 1,000-case runs took approximately 1–17 seconds per institution when run concurrently. These are observed performance figures, not estimates of classification accuracy. Live model-mode performance has not been measured. Exact fresh-run hashes and timing are in `system/reports/verification.json`; performance on the unseen 200-case folders may differ.

Runtime audit traces are written to `system/reports/<institution>.audit.jsonl`; summary timing, decision counts and model usage are written to `<institution>.metrics.json`. Audit files are generated by runs and are not required or bundled as classifier knowledge.

## Files

- `run`, `run.cmd`, `cli.py`: portable entry points, bounded model orchestration and JSONL output.
- `domain.py`: data joins, Markdown parsing, money/date handling and result validation.
- `engine.py`: per-alert evidence checks, policy boundaries and decision aggregation.
- `matching.py`: dated/reference-linked financial evidence, row capacity and one-to-one allocation.
- `model.py`: optional OpenRouter analysis and validation.
- `serve.py`, `viewer/`: local analyst interface.
- `test_system.py`, `test_evidence_integrity.py`, `verify.py`: synthetic policy/regression tests and full-output/reproducibility checks.

Python 3.10 or later is required. No installed third-party dependencies are used.
