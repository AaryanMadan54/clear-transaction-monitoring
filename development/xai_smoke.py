"""Explicit development-only xAI smoke run. Not included in submission.zip."""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system"))
from domain import Institution, validate_result
from engine import evaluate
from model import ModelReview, apply_review
from matching import FINANCIAL_TYPES


def load_key(path):
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and key.strip() in {"XAI_API_KEY", "OPENROUTER_API_KEY"}:
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class DevelopmentReview(ModelReview):
    def __init__(self, budget=0.50, max_calls=12):
        super().__init__(model="grok-4.3", input_rate="1.25", output_rate="2.50",
                         budget=budget, max_calls=max_calls)
        self.provider = "xai-development"
        self.key_variable = "XAI_API_KEY"
        self.key = os.environ.get(self.key_variable, "").strip()
        self.endpoint = "https://api.x.ai/v1/chat/completions"

    def request_options(self):
        return {"reasoning_effort": "none"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--institution", choices=["bank", "emoney", "investment", "payments_fx"], default="bank")
    parser.add_argument("--limit", type=int, default=8, help="Number of eligible cases, not the first N cases")
    parser.add_argument("--budget", type=float, default=0.50)
    parser.add_argument("--financial-evidence", action="store_true", help="Exercise document-rich unresolved cases")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    if not 1 <= args.limit <= 24 or not 0 < args.budget <= 2:
        parser.error("Use 1..24 cases and a budget greater than zero and no more than $2")
    load_key(args.env_file)
    if not os.environ.get("XAI_API_KEY"):
        parser.error("XAI_API_KEY is absent from the process and the selected .env file")
    reviewer = DevelopmentReview(args.budget, args.limit * 2)
    institution = Institution(ROOT / "institutions" / args.institution)
    rows = []
    for case in sorted(institution.cases, key=lambda c: c["case_id"]):
        ctx = institution.context(case)
        if args.financial_evidence and not any(d.type in FINANCIAL_TYPES for d in ctx.docs):
            continue
        baseline, findings = evaluate(ctx)
        if not any(not f.supported for f in findings) or any(f.hard and f.verdict == "ESCALATE" for f in findings):
            continue
        review = reviewer.review(ctx, baseline, findings)
        result, checks = apply_review(ctx, baseline, findings, review)
        validate_result(result, ctx)
        row = {"case_id": case["case_id"], "baseline": baseline, "result": result,
               "checks": [f.as_dict() for f in checks], "model_review": review}
        rows.append(row)
        print(json.dumps({"review": len(rows), "status": review["status"],
                          "error_code": review.get("error_code"), "message": review.get("message"),
                          "baseline": baseline["verdict"], "result": result["verdict"],
                          "closure_rejections": len(review.get("rejected_closures", []))}), flush=True)
        if len(rows) >= args.limit or review["status"] in {"service_skipped", "budget_skipped"}:
            break
    report = {"provider": reviewer.provider, "model": reviewer.model, "cases": len(rows),
              "financial_evidence_only": args.financial_evidence,
              "statuses": dict(collections.Counter(r["model_review"]["status"] for r in rows)),
              "changed_results": sum(r["result"] != r["baseline"] for r in rows),
              "changed_verdicts": sum(r["result"]["verdict"] != r["baseline"]["verdict"] for r in rows),
              "calls": reviewer.calls, "cost_usd": float(reviewer.cost),
              "cost_upper_bound_usd": float(reviewer.reserved),
              "accuracy": "unmeasured: no reference labels", "submission_endpoint_unchanged": True}
    reports = ROOT / "system" / "reports"
    reports.mkdir(exist_ok=True)
    label = args.institution + (".financial" if args.financial_evidence else "")
    (reports / (label + ".xai-smoke.json")).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (reports / (label + ".xai-smoke.audit.jsonl")).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    print(json.dumps(report), flush=True)
    return 0 if rows and all(r["model_review"]["status"] == "reviewed" for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
