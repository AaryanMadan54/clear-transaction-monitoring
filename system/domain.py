"""Case loading and evidence parsing. Standard library only; no training-case data."""
from __future__ import annotations

import collections
import datetime as dt
import json
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

REASON_CODES = {
    "CLOSE": set("COLLECTION_DOCUMENTED BUSINESS_RECORDS_RECONCILE DOCUMENTED_EVENT INVOICE_MATCHES OWN_ACCOUNT GROUP_COMPANY THIRD_PARTY_DOCUMENTED REFUND_OF_EARLIER_PAYMENT RETURNING_CUSTOMER LIST_MATCH_DIFFERENT_PARTY WITHIN_PROFILE".split()),
    "RFI": set("RFI_PURPOSE_DOCUMENTS RFI_BUSINESS_RECORDS RFI_SOURCE_OF_FUNDS RFI_COUNTERPARTY_DETAILS RFI_UPDATED_PROFILE".split()),
    "ESCALATE": set("ESC_MULE_ACTIVITY ESC_STRUCTURING ESC_UNEXPLAINED_FUNDS ESC_PASS_THROUGH ESC_INVOICE_MISMATCH ESC_HIGH_RISK_CORRIDOR ESC_ACCOUNT_MISUSE ESC_THIRD_PARTY_FUNDING ESC_EARLY_REDEMPTION ESC_LISTED_PARTY".split()),
}
SEVERITY = {"CLOSE": 0, "RFI": 1, "ESCALATE": 2}


def norm(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def contains(text, value):
    return bool(norm(value)) and (" " + norm(value) + " ") in (" " + norm(text) + " ")


def cents(value):
    try:
        return int((Decimal(str(value).replace(",", "").replace("£", "").strip()) * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError):
        return None


def money(value):
    return f"£{Decimal(str(value)):,.2f}"


def amounts(text):
    return [cents(m) for m in re.findall(r"(?:£|GBP\s*)([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text, re.I)]


def date(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%B %Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(str(value).strip()[:10] if fmt == "%Y-%m-%d" else str(value).strip(), fmt).date()
        except ValueError:
            pass
    return None


def same_amount(a, b):
    return a is not None and b is not None and abs(a - b) <= 1


@dataclass
class Document:
    meta: dict
    text: str
    fields: dict = field(init=False)
    tables: list = field(init=False)
    amounts: list = field(init=False)

    def __post_init__(self):
        self.fields = {}
        for key, val in re.findall(r"^\*\*([^*]+?):\*\*\s*(.*?)\s*$", self.text, re.M):
            self.fields[norm(key)] = val.strip()
        self.amounts = amounts(self.text)
        self.tables = []
        header = None
        for line in self.text.splitlines():
            if not line.strip().startswith("|"):
                header = None
                continue
            cells = [v.strip() for v in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r"[-: ]*", v) for v in cells):
                continue
            if header is None:
                header = [norm(v) for v in cells]
            else:
                self.tables.append(dict(zip(header, cells)))

    @property
    def id(self):
        return self.meta["doc_id"]

    @property
    def type(self):
        return self.meta["type"]

    def get(self, *keys):
        return next((self.fields[norm(k)] for k in keys if norm(k) in self.fields), "")

    def amount_party_matches(self, transaction, party=True):
        amount_ok = any(same_amount(v, cents(transaction["amount_gbp"])) for v in self.amounts)
        counterparty = transaction.get("counterparty_name") or ""
        # Solicitor and conveyancer payment account names can append "client account".
        legal_party = re.sub(r"\s+client\s+account$", "", counterparty, flags=re.I)
        party_ok = not party or contains(self.text, counterparty) or (legal_party != counterparty and contains(self.text, legal_party))
        account = transaction.get("counterparty_account")
        account_ok = account and len(norm(account)) >= 4 and contains(self.text, account)
        return amount_ok and (party_ok or account_ok)

    def matches(self, transaction, party=True):
        # Keep the amount/party predicate available for identifying failed coverage;
        # closure callers must also establish a date or transaction-reference link.
        from matching import document_link
        return self.amount_party_matches(transaction, party) and document_link(self, transaction)


@dataclass
class Finding:
    check: str
    verdict: str
    code: str
    detail: str
    evidence: list
    hard: bool = False
    supported: bool = True

    def as_dict(self):
        return {"check": self.check, "verdict": self.verdict, "reason_code": self.code,
                "detail": self.detail, "evidence": sorted(set(self.evidence)),
                "policy_veto": self.hard, "supported": self.supported}


class Institution:
    def __init__(self, folder):
        self.root = Path(folder).resolve(strict=True)
        self.name = self.root.name
        if self.name not in {"bank", "emoney", "investment", "payments_fx"}:
            raise ValueError("Institution folder must be bank, emoney, investment or payments_fx")
        self.policy = self._file("POLICY.md").read_text(encoding="utf-8-sig")
        self.cases = self.read("cases")
        self.customers = self.index(self.read("customers"), "customer_id")
        self.alerts = self.index(self.read("alerts"), "alert_id")
        self.transactions = self.index(self.read("transactions"), "transaction_id")
        self.list_entries = self.index(self.read("internal_list"), "entry_id")
        self.tx_by_customer = self.group(self.transactions.values(), "customer_id")
        self.docs_by_customer = self.group(self.read("evidence"), "customer_id")
        self.reviews_by_customer = self.group(self.read("previous_reviews"), "customer_id")
        self.doc_cache = {}
        match = re.search(r"own accounts explain activity at\s+(\d+)\s+times", self.policy, re.I)
        if not match:
            raise ValueError("Cannot read own-account profile limit from POLICY.md")
        self.own_profile_limit = Decimal(match.group(1))
        match = re.search(r"Payments of £([\d,]+) or more to anyone other than", self.policy)
        self.invoice_limit = cents(match.group(1)) if match else None

    def _file(self, relative):
        path = (self.root / relative).resolve(strict=True)
        if not path.is_relative_to(self.root):
            raise ValueError("Evidence path escapes institution folder")
        return path

    def read(self, name):
        rows = []
        with self._file(name + ".jsonl").open(encoding="utf-8-sig") as source:
            for number, line in enumerate(source, 1):
                if line.strip():
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError(f"{name}:{number} must contain an object")
                    rows.append(row)
        return rows

    @staticmethod
    def index(rows, key):
        result = {}
        for row in rows:
            if row[key] in result:
                raise ValueError(f"Duplicate {key}: {row[key]}")
            result[row[key]] = row
        return result

    @staticmethod
    def group(rows, key):
        result = collections.defaultdict(list)
        for row in rows:
            result[row[key]].append(row)
        return result

    def context(self, case):
        customer_id = case["customer_id"]
        docs = []
        for meta in sorted(self.docs_by_customer[customer_id], key=lambda d: d["doc_id"]):
            if meta["doc_id"] not in self.doc_cache:
                self.doc_cache[meta["doc_id"]] = Document(meta, self._file(meta["file"]).read_text(encoding="utf-8-sig"))
            docs.append(self.doc_cache[meta["doc_id"]])
        alerts = [self.alerts[a] for a in sorted(case["alert_ids"])]
        for alert in alerts:
            if alert["case_id"] != case["case_id"] or alert["customer_id"] != customer_id:
                raise ValueError("Alert does not belong to case")
        return Context(self, case, self.customers[customer_id], alerts, docs,
                       sorted(self.tx_by_customer[customer_id], key=lambda t: (t["booked_at"], t["transaction_id"])),
                       sorted(self.reviews_by_customer[customer_id], key=lambda r: (r["reviewed_on"], r["review_id"])))


@dataclass
class Context:
    institution: Institution
    case: dict
    customer: dict
    alerts: list
    docs: list
    transactions: list
    reviews: list

    @property
    def name(self):
        return self.customer["name"]

    @property
    def rules(self):
        return {a["rule_id"] for a in self.alerts}

    def documents(self, *types):
        return [d for d in self.docs if d.type in types]

    def tx(self, alert=None):
        ids = set(alert["transaction_ids"] if alert else (t for a in self.alerts for t in a["transaction_ids"]))
        return [t for t in self.transactions if t["transaction_id"] in ids]

    def own_documents(self, transaction):
        result = []
        for d in self.documents("bank_statement"):
            holder = d.get("Account holder", "Account holders")
            account = transaction.get("counterparty_account")
            account_ok = bool(account) and contains(d.text, account)
            owner_ok = norm(holder) == norm(self.name) or ("account holders" in d.fields and contains(holder, self.name))
            if owner_ok and (account_ok or (d.matches(transaction) and contains(transaction.get("counterparty_name"), self.name))):
                result.append(d)
        return result

    def legal_evidence(self):
        # Previous-review IDs are kept in the audit trace, not the submission evidence list.
        return {d.id for d in self.docs} | {t["transaction_id"] for t in self.transactions} | {
            a["measured"]["list_entry"] for a in self.alerts if a.get("measured", {}).get("list_entry") in self.institution.list_entries}

    def pack(self):
        return {"case": self.case, "customer": self.customer, "alerts": self.alerts,
                "transactions": self.transactions, "documents": [dict(d.meta, content=d.text) for d in self.docs],
                "previous_reviews": self.reviews,
                "internal_list": [self.institution.list_entries[k] for k in sorted(self.legal_evidence()) if k in self.institution.list_entries]}


def validate_result(result, ctx):
    expected = {"case_id", "alert_ids", "verdict", "reason_code", "reason", "evidence"}
    if set(result) != expected or result["case_id"] != ctx.case["case_id"]:
        raise ValueError("Invalid result fields or case ID")
    if sorted(result["alert_ids"]) != sorted(ctx.case["alert_ids"]) or len(set(result["alert_ids"])) != len(result["alert_ids"]):
        raise ValueError("Result must cover every alert exactly once")
    if result["verdict"] not in REASON_CODES or result["reason_code"] not in REASON_CODES[result["verdict"]]:
        raise ValueError("Invalid verdict/reason-code pair")
    if not isinstance(result["reason"], str) or not result["reason"].strip():
        raise ValueError("Missing explanation")
    if not isinstance(result["evidence"], list) or not all(isinstance(e, str) for e in result["evidence"]):
        raise ValueError("Evidence must be an array of IDs")
    if len(result["evidence"]) != len(set(result["evidence"])) or not set(result["evidence"]) <= ctx.legal_evidence():
        raise ValueError("Unrelated, duplicate or nonexistent evidence citation")
    if not result["evidence"]:
        raise ValueError("A verdict must cite evidence")
    if result["verdict"] == "RFI" and not re.search(r"\b(?:ask|request|obtain)\b", result["reason"], re.I):
        raise ValueError("RFI must specify a request")
    return result
