#!/usr/bin/env python3
"""Local analyst demo. Separate from the submission entry point; no dependencies."""
from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from domain import Institution, validate_result
from engine import evaluate
from model import ModelReview
from configuration import PROVIDERS, load_environment, selected_provider

SYSTEM = Path(__file__).resolve().parent
NAMES = {"bank": "Bank", "emoney": "E-money", "investment": "Investment", "payments_fx": "Payments & FX"}


class Store:
    def __init__(self, workspace):
        self.workspace = workspace.resolve()
        self.institutions = {}
        self.lock = threading.RLock()
        self.job = {"state": "idle"}

    def institution(self, name):
        if name not in NAMES:
            raise ValueError("Unknown institution")
        with self.lock:
            if name not in self.institutions:
                self.institutions[name] = Institution(self.workspace / "institutions" / name)
            return self.institutions[name]

    def outputs(self, name):
        if name not in NAMES:
            raise ValueError("Unknown institution")
        path = self.workspace / "output" / (name + ".jsonl")
        if not path.exists():
            return {}
        return {r["case_id"]: r for r in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)}

    def summary(self):
        rows = []
        for name, label in NAMES.items():
            outputs = self.outputs(name)
            counts = collections.Counter(r["verdict"] for r in outputs.values())
            metrics_path = SYSTEM / "reports" / (name + ".metrics.json")
            metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.exists() else {}
            rows.append({"id": name, "label": label, "cases": len(outputs), "counts": counts, "metrics": metrics})
        provider = selected_provider()
        return {"institutions": rows, "model_available": bool(os.environ.get(PROVIDERS[provider]["key"])),
                "provider": provider, "model": os.environ.get("TM_MODEL") or PROVIDERS[provider]["model"], "job": self.job}

    def cases(self, name):
        inst = self.institution(name)
        outputs = self.outputs(name)
        rows = []
        for case in sorted(inst.cases, key=lambda c: c["case_id"]):
            result = outputs.get(case["case_id"])
            customer = inst.customers[case["customer_id"]]
            alerts = [inst.alerts[k] for k in case["alert_ids"]]
            rows.append({"case_id": case["case_id"], "customer": customer["name"], "kind": customer["kind"],
                         "verdict": result["verdict"] if result else "PENDING", "reason_code": result["reason_code"] if result else "",
                         "rules": sorted({a["rule_id"] for a in alerts}), "alert_count": len(alerts),
                         "evidence_count": len(result["evidence"]) if result else 0,
                         "date": case["review_window"]["to"]})
        return {"cases": rows}

    def detail(self, name, key):
        inst = self.institution(name)
        case = next((c for c in inst.cases if c["case_id"] == key), None)
        if case is None:
            raise LookupError("Case not found")
        ctx = inst.context(case)
        baseline, findings = evaluate(ctx)
        saved = self.outputs(name).get(key)
        if saved:
            validate_result(saved, ctx)
        audit_path = SYSTEM / "reports" / (name + ".audit.jsonl")
        audit = None
        if audit_path.exists():
            for line in audit_path.read_text(encoding="utf-8").splitlines():
                candidate = json.loads(line)
                if candidate["case_id"] == key:
                    audit = candidate
                    break
        return {**ctx.pack(), "result": saved or baseline, "baseline": baseline,
                "checks": audit["checks"] if audit and audit.get("result") == saved else [f.as_dict() for f in findings],
                "mode": audit.get("mode", "rules") if audit and audit.get("result") == saved else "rules",
                "model_review": audit.get("model_review") if audit else None, "export_available": saved is not None}

    def run(self):
        with self.lock:
            if self.job["state"] == "running":
                raise ValueError("A run is already in progress")
            self.job = {"state": "running", "completed": 0, "total": 4, "institution": "bank", "mode": "rules"}
        def work():
            try:
                for i, name in enumerate(NAMES):
                    self.job.update(institution=name)
                    command = [sys.executable, str(SYSTEM / "run"), str(self.workspace / "institutions" / name), str(self.workspace / "output" / (name + ".jsonl")), "--mode", "rules"]
                    run = subprocess.run(command, cwd=str(SYSTEM), capture_output=True, text=True, encoding="utf-8", timeout=1800)
                    if run.returncode:
                        raise RuntimeError(f"{name} run failed; inspect the terminal")
                    self.job.update(completed=i + 1)
                self.job.update(state="complete")
            except Exception as error:
                self.job.update(state="failed", message=str(error))
        threading.Thread(target=work, daemon=True).start()
        return self.job


class Handler(BaseHTTPRequestHandler):
    server_version = "AlertReview/1.0"

    def log_message(self, fmt, *args):
        # No request payloads or model credentials are logged.
        pass

    def send(self, value, status=200, content_type="application/json; charset=utf-8", filename=None):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8") if isinstance(value, (dict, list)) else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        get = lambda key, default="": query.get(key, [default])[0]
        try:
            if parsed.path == "/api/summary":
                return self.send(self.server.store.summary())
            if parsed.path == "/api/cases":
                return self.send(self.server.store.cases(get("institution")))
            if parsed.path == "/api/case":
                return self.send(self.server.store.detail(get("institution"), get("id")))
            if parsed.path == "/api/job":
                return self.send(self.server.store.job)
            if parsed.path == "/api/policy":
                return self.send({"policy": self.server.store.institution(get("institution")).policy})
            if parsed.path == "/api/export":
                name = get("institution")
                inst = self.server.store.institution(name)
                outputs = self.server.store.outputs(name)
                if len(outputs) != len(inst.cases):
                    raise ValueError("Complete results are not available; run the institution first")
                data = "".join(json.dumps(outputs[c["case_id"]], ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for c in sorted(inst.cases, key=lambda c: c["case_id"]))
                return self.send(data.encode("utf-8"), content_type="application/x-ndjson; charset=utf-8", filename=name + ".jsonl")
            assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
            if parsed.path in assets:
                filename, mime = assets[parsed.path]
                return self.send((SYSTEM / "viewer" / filename).read_bytes(), content_type=mime)
            self.send({"error": "Not found"}, 404)
        except (ValueError, KeyError, LookupError) as error:
            self.send({"error": str(error)}, 400)
        except (OSError, json.JSONDecodeError):
            self.send({"error": "Unable to read the local dataset or results"}, 500)

    def do_POST(self):
        try:
            # Only our own local page may trigger work. Keys always stay server-side.
            origin = self.headers.get("Origin")
            if self.headers.get("X-Alert-Review") != "local" or (origin and urlparse(origin).netloc != self.headers.get("Host")):
                return self.send({"error": "Local application request required"}, 403)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 8192:
                raise ValueError("Request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/run":
                return self.send(self.server.store.run(), 202)
            if self.path == "/api/review":
                provider = selected_provider()
                if not os.environ.get(PROVIDERS[provider]["key"]):
                    return self.send({"error": "Set " + PROVIDERS[provider]["key"] + " to enable AI review"}, 409)
                name, key = body.get("institution"), body.get("case_id")
                inst = self.server.store.institution(name)
                case = next((c for c in inst.cases if c["case_id"] == key), None)
                if case is None:
                    raise ValueError("Case not found")
                ctx = inst.context(case)
                result, findings = evaluate(ctx)
                review = ModelReview(model=os.environ.get("TM_MODEL"), provider=provider, budget=0.10, max_calls=1).review(ctx, result, findings)
                return self.send(review)
            self.send({"error": "Not found"}, 404)
        except (ValueError, KeyError, TypeError) as error:
            self.send({"error": str(error)}, 400)


def main():
    parser = argparse.ArgumentParser(description="Open the local alert-review dashboard")
    parser.add_argument("--workspace", type=Path, default=SYSTEM.parent)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--provider", choices=["openrouter", "xai"])
    parser.add_argument("--model")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    try:
        load_environment(args.env_file)
        os.environ["TM_PROVIDER"] = selected_provider(args.provider)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    if args.model:
        os.environ["TM_MODEL"] = args.model
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.store = Store(args.workspace)
    print(f"Alert Review is running at http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
