"""Stable JSONL output, audit trail and bounded optional model review."""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from domain import Institution, validate_result
from engine import evaluate
from model import DEFAULT_MODEL, ModelReview, apply_review

SYSTEM_DIR = Path(__file__).resolve().parent


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Review all transaction-monitoring cases in an institution folder")
    parser.add_argument("institution_folder", type=Path)
    parser.add_argument("output_file", type=Path)
    parser.add_argument("--mode", choices=["rules", "advisory", "hybrid"], default=os.environ.get("TM_MODE", "rules"),
                        help="rules: local policy engine; advisory: AI second opinion only; hybrid: guarded AI adjudication")
    parser.add_argument("--model", default=os.environ.get("TM_MODEL", DEFAULT_MODEL))
    parser.add_argument("--provider", choices=["openrouter"], default="openrouter",
                        help="Model requests use OpenRouter exclusively")
    parser.add_argument("--budget", type=float, default=3.50, help="Conservative model spend ceiling per 200 cases, USD")
    parser.add_argument("--limit", type=int, help="Development benchmark only: process the first N cases")
    parser.add_argument("--no-audit", action="store_true")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if not 0 < args.budget <= 4:
        parser.error("--budget must be greater than zero and no more than $4 per 200 cases")
    key_variable = "OPENROUTER_API_KEY"
    if args.mode != "rules" and not os.environ.get(key_variable):
        parser.error("This model mode requires " + key_variable + "; use --mode rules to run locally")
    try:
        reviewer = ModelReview(args.model, args.budget, provider=args.provider) if args.mode != "rules" else None
    except ValueError as error:
        parser.error(str(error))
    institution = Institution(args.institution_folder)
    cases = sorted(institution.cases, key=lambda c: c["case_id"])
    if args.limit:
        cases = cases[:args.limit]
    output = args.output_file.resolve()
    if output.is_relative_to(institution.root):
        parser.error("Output must not overwrite files in the input institution folder")
    output.parent.mkdir(parents=True, exist_ok=True)
    audit = None
    if not args.no_audit:
        reports = SYSTEM_DIR / "reports"
        reports.mkdir(exist_ok=True)
        audit = (reports / (institution.name + ".audit.jsonl")).open("w", encoding="utf-8", newline="\n")
    counts = collections.Counter()
    review_counts = collections.Counter()
    total_model_calls = 0
    total_model_cost = 0.0
    total_model_reserved = 0.0
    model_errors = collections.Counter()
    temporary = None
    try:
        # Atomic replacement prevents a failed run from leaving a misleading partially complete submission.
        temporary = SYSTEM_DIR / ("." + institution.name + ".output-" + str(os.getpid()) + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            def process(ctx):
                result, findings = evaluate(ctx)
                baseline = result
                review = {"status": "not_requested"}
                needs_review = any(not f.supported for f in findings) and not any(f.hard and f.verdict == "ESCALATE" for f in findings)
                if reviewer and (args.mode == "advisory" or needs_review):
                    review = reviewer.review(ctx, result, findings)
                    if args.mode == "hybrid":
                        result, findings = apply_review(ctx, result, findings, review)
                validate_result(result, ctx)
                return ctx, result, baseline, findings, review

            for start in range(0, len(cases), 200):
                if reviewer and start:
                    total_model_calls += reviewer.calls
                    total_model_cost += float(reviewer.cost)
                    total_model_reserved += float(reviewer.reserved)
                    reviewer = reviewer.next_block()
                contexts = [institution.context(case) for case in cases[start:start + 200]]
                # Six bounded workers keep a 200-case block below 30 minutes even if
                # all 200 model requests exhaust the 25-second request timeout.
                with ThreadPoolExecutor(max_workers=6 if reviewer else 1) as executor:
                    for ctx, result, baseline, findings, review in executor.map(process, contexts):
                        stream.write(dumps(result) + "\n")
                        if audit:
                            audit.write(dumps({"case_id": ctx.case["case_id"], "mode": args.mode,
                                               "baseline": baseline, "result": result,
                                               "checks": [f.as_dict() for f in findings], "model_review": review}) + "\n")
                        counts[result["verdict"]] += 1
                        review_counts[review["status"]] += 1
                        if review.get("error_code"):
                            model_errors[review["error_code"]] += 1
        # os.replace also works between volumes only when the OS permits it; full paths here normally share a volume.
        try:
            os.replace(temporary, output)
        except OSError:
            # The permitted output may be on a different volume. Write that exact file without creating sibling files.
            with temporary.open("rb") as source, output.open("wb") as target:
                import shutil
                shutil.copyfileobj(source, target)
            temporary.unlink()
    finally:
        if audit:
            audit.close()
        if temporary is not None and temporary.exists():
            temporary.unlink()
    if reviewer:
        total_model_calls += reviewer.calls
        total_model_cost += float(reviewer.cost)
        total_model_reserved += float(reviewer.reserved)
    report = {"institution": institution.name, "cases": len(cases), "mode": args.mode, "provider": args.provider,
              "verdicts": dict(sorted(counts.items())), "seconds": round(time.perf_counter() - started, 3),
              "model_calls": total_model_calls, "model_cost_usd": round(total_model_cost, 6),
              "model_cost_upper_bound_usd": round(total_model_reserved, 6),
              "model_errors": dict(sorted(model_errors.items())),
              "model_review_statuses": dict(sorted(review_counts.items())),
              "accuracy": "not measured: development cases have no answer labels"}
    if not args.no_audit:
        (SYSTEM_DIR / "reports" / (institution.name + ".metrics.json")).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(dumps(report), file=sys.stderr)
    return 0
