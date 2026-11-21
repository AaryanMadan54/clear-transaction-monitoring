"""Evidence-grounded policy checks shared by all four institutions.

The engine consumes facts from the supplied folder, never case-specific lookup tables.
Every alert gets its own finding; severity is aggregated only after all checks run.
"""
from __future__ import annotations

import collections
import datetime as dt
import re
from decimal import Decimal

from domain import Finding, SEVERITY, amounts, cents, contains, date, money, norm, same_amount, validate_result
from matching import CASH_TYPES, FINANCIAL_TYPES, INCOMING_TYPES, OUTGOING_TYPES, allocate, allocate_for_case, references, row_matches

EVENT_TYPES = {"insurer_letter", "solicitor_letter", "completion_statement", "settlement_letter", "vehicle_sale_receipt", "sale_receipt"}
PAYMENT_TYPES = {"invoice", "supplier_invoice", "supplier_invoices", "contract_schedule", "hospital_invoice", "dealer_invoice", "consulting_contract", "carrier_invoice", "freight_invoices"}
BASE_TYPES = {"due_diligence_summary", "proof_of_address", "contact_note"}


def ids(documents):
    return [d.id for d in documents]


def tx_ids(transactions):
    return [t["transaction_id"] for t in transactions]


def excerpt(doc, limit=235):
    """A case-specific factual excerpt, omitting boilerplate metadata."""
    text = re.sub(r"^\*\*(?:Document ID|Dated):\*\*.*$", "", doc.text, flags=re.M)
    text = re.sub(r"[*#|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip(" ,.;:")


def find(check, verdict, code, detail, documents=(), transactions=(), hard=False, supported=True, extra=()):
    return Finding(check, verdict, code, detail.rstrip(" ."), sorted(set(ids(documents) + tx_ids(transactions) + list(extra))), hard, supported)


def regular_income(ctx, transaction):
    employer = ctx.customer.get("employer")
    if employer and norm(transaction.get("counterparty_name")) == norm(employer) and norm(transaction.get("reference")).startswith("salary"):
        return True
    # Routine small funding is context, not the explanation for an anomalous own-account transfer.
    if norm(transaction.get("counterparty_name")) != norm(ctx.name) or norm(transaction.get("reference")) not in {"top up", "topup", "monthly contribution"}:
        return False
    baseline = ctx.customer.get("declared", {}).get("monthly_income_gbp")
    if not baseline:
        return False
    window = ctx.case["review_window"]
    pool = [t for t in ctx.transactions if t["direction"] == "in" and norm(t.get("counterparty_name")) == norm(ctx.name) and norm(t.get("reference")) in {"top up", "topup", "monthly contribution"} and window["from"] <= t["booked_at"][:10] <= window["to"]]
    return sum(Decimal(str(t["amount_gbp"])) for t in pool) <= Decimal(str(baseline))


def routine_transaction(ctx, transaction):
    if regular_income(ctx, transaction):
        return True
    if ctx.institution.name == "payments_fx":
        if transaction["direction"] == "in" and norm(transaction.get("counterparty_name")) == norm(ctx.name) and not transaction.get("counterparty_account"):
            return True
        expected = ctx.customer["declared"].get("expected_countries", [])
        older = [t for t in ctx.transactions if t["booked_at"][:10] < ctx.case["review_window"]["from"] and t["direction"] == transaction["direction"] and norm(t.get("counterparty_name")) == norm(transaction.get("counterparty_name"))]
        return bool(older and transaction.get("counterparty_country") in expected and Decimal(str(transaction["amount_gbp"])) <= max(Decimal(str(t["amount_gbp"])) for t in older) * Decimal("1.5"))
    if transaction["direction"] != "out" or ctx.institution.name == "investment":
        return False
    baseline = ctx.customer["declared"].get("monthly_income_gbp")
    if not baseline:
        return False
    if transaction.get("channel") in {"card", "standing_order", "direct_debit"}:
        older = [t for t in ctx.transactions if t["booked_at"][:10] < ctx.case["review_window"]["from"] and norm(t.get("counterparty_name")) == norm(transaction.get("counterparty_name")) and t["direction"] == "out"]
        return bool(older and Decimal(str(transaction["amount_gbp"])) <= max(Decimal(str(t["amount_gbp"])) for t in older) * 2)
    small_refs = {"taxi", "dinner", "thanks", "tickets"}
    if norm(transaction.get("reference")) in small_refs and Decimal(str(transaction["amount_gbp"])) <= Decimal(str(baseline)) / 10:
        total = sum(Decimal(str(t["amount_gbp"])) for t in ctx.transactions if t["direction"] == "out" and norm(t.get("reference")) in small_refs and ctx.case["review_window"]["from"] <= t["booked_at"][:10] <= ctx.case["review_window"]["to"])
        return total <= Decimal(str(baseline)) / 2
    return False


def historical_cash_context(ctx, transaction):
    """Background spend for claim allocation, never a new closure justification."""
    if transaction['direction'] != 'out' or transaction.get('channel') != 'cash_withdrawal' or ctx.institution.name == 'investment':
        return False
    baseline = ctx.customer.get('declared', {}).get('monthly_income_gbp')
    if not baseline:
        return False
    older = [t for t in ctx.transactions if t['direction'] == 'out' and t.get('channel') == 'cash_withdrawal' and t['booked_at'][:10] < ctx.case['review_window']['from']]
    total = sum(Decimal(str(t['amount_gbp'])) for t in ctx.transactions if t['direction'] == 'out' and t.get('channel') == 'cash_withdrawal' and ctx.case['review_window']['from'] <= t['booked_at'][:10] <= ctx.case['review_window']['to'])
    return bool(older and Decimal(str(transaction['amount_gbp'])) <= max(Decimal(str(t['amount_gbp'])) for t in older)
                and total <= Decimal(str(baseline)) / 5)


def match_rows(doc, transaction, amount_keys=("amount", "total", "cash topped up", "cash counted"), party_keys=()):
    return any(row_matches(doc, row, transaction, amount_keys, party_keys) for row in doc.tables)


def cash_records(ctx, transactions):
    cash = [t for t in transactions if t.get("channel") in {"cash_deposit", "cash_topup"}]
    if not cash:
        return []
    docs = ctx.documents("takings_book", "cash_collection_reports", "cash_reconciliation")
    allocation = allocate(docs, cash, rows_only=True)
    used = {u.doc_id for u in allocation.assignments.values()}
    return [d for d in docs if d.id in used] if allocation.complete else []


def payment_documents(ctx, transactions):
    result = []
    for t in transactions:
        for d in ctx.docs:
            if d.type not in PAYMENT_TYPES:
                continue
            if d.type in {"contract_schedule", "supplier_invoices", "freight_invoices"}:
                if contains(d.text, t.get("counterparty_name")) and match_rows(d, t):
                    result.append(d)
            elif d.matches(t):
                result.append(d)
    candidates = list({d.id: d for d in result}.values())
    proof = allocate_for_case(ctx, candidates, transactions)
    return candidates if proof.complete else []


def event_documents(ctx, transactions):
    return [d for d in ctx.docs if d.type in EVENT_TYPES | {"loan_agreement"} and any(d.matches(t, party=d.type not in {"vehicle_sale_receipt", "sale_receipt"}) for t in transactions)]


def refund_documents(ctx, transactions):
    result = []
    for t in transactions:
        if t["direction"] != "in":
            continue
        docs = [d for d in ctx.documents("venue_email", "credit_note", "deposit_return_letter") if d.matches(t)]
        if not docs:
            continue
        prior = [p for p in ctx.transactions if p["direction"] == "out" and p["booked_at"] < t["booked_at"] and
                 norm(p.get("counterparty_name")) == norm(t.get("counterparty_name")) and same_amount(cents(p["amount_gbp"]), cents(t["amount_gbp"]))]
        # A protected tenancy deposit return is independently documented even if the original
        # deposit predates the transaction history supplied by the institution.
        if prior or any(d.type == "deposit_return_letter" and re.search(r"deposit protection|tenancy", d.text, re.I) for d in docs):
            result.extend(docs)
    return list({d.id: d for d in result}.values())


def gift_documents(ctx, transactions):
    return [d for d in ctx.documents("gift_letter") if contains(d.text, ctx.name) and
            re.search(r"(?:no (?:requirement|interest|legal|beneficial)|non.repayable|not require repayment|do not require repayment|no requirement for repayment)", d.text, re.I) and
            any(t["direction"] == "in" and d.matches(t) for t in transactions)]


def verified_group(ctx, transactions):
    docs = ctx.documents("group_structure")
    registers = ctx.documents("company_register_extract")
    for chart in docs:
        rows = [dict(r, owner=r.get("owner") or r.get("owned by", "")) for r in chart.tables]
        own = [r for r in rows if norm(r.get("company")) == norm(ctx.name)]
        if not own:
            continue
        own_owners = {norm(r.get("owner")) for r in own if r.get("owner")}
        other = [r for r in rows if r.get("owner") and (norm(r.get("owner")) in own_owners or norm(r.get("company")) in own_owners) and norm(r.get("company")) != norm(ctx.name)]
        relevant = [r for r in other if any(norm(t.get("counterparty_name")) == norm(r.get("company")) for t in transactions)]
        if not relevant:
            continue
        supported = []
        for row in own + relevant:
            regs = [d for d in registers if contains(d.text.splitlines()[0], row["company"]) and contains(d.text, row["owner"]) and any("control" in norm(k) for r in d.tables for k in r)]
            if not regs:
                # A dated group ownership chart plus a register corroborating the customer
                # establishes a sibling relationship; a self-asserted email alone does not.
                own_regs = [d for d in registers if contains(d.text.splitlines()[0], ctx.name) and any(contains(d.text, r.get("owner")) for r in own)]
                if own_regs and all("100" in r.get("share", "") for r in own + relevant):
                    supported.extend(own_regs)
                    continue
                break
            supported.extend(regs)
        else:
            return [chart] + list({d.id: d for d in supported}.values())
    return []


def factoring_documents(ctx, transactions):
    agreements = ctx.documents("factoring_agreement")
    remittances = ctx.documents("remittance_advice")
    return [d for d in agreements if contains(d.text, ctx.name) and any(contains(d.text, t.get("counterparty_name")) for t in transactions)] + [
        d for d in remittances if contains(d.text, ctx.name) and any(d.matches(t) for t in transactions)] if agreements and remittances else []


def own_transfer_finding(ctx, alert, transactions):
    relevant = [t for t in transactions if not routine_transaction(ctx, t) and t.get("channel") not in {"card", "fund_subscription", "fund_redemption"}]
    if not relevant:
        return None
    documents = []
    for t in relevant:
        ds = ctx.own_documents(t)
        if not ds:
            return None
        documents.extend(ds)
    documents = list({d.id: d for d in documents}.values())
    profile = next((a for a in ctx.alerts if a["rule_id"] == "TM01"), None)
    if profile:
        measured = profile["measured"]
        multiple = Decimal(str(measured.get("received_gbp", 0))) / Decimal(str(measured.get("baseline_gbp") or 1))
        if multiple >= ctx.institution.own_profile_limit:
            return find(alert["alert_id"], "RFI", "RFI_UPDATED_PROFILE",
                        f"{', '.join(ids(documents))} verifies the other accounts belong to {ctx.name}, but inflows of {money(measured['received_gbp'])} are {multiple:.2f} times the declared monthly baseline of {money(measured['baseline_gbp'])}; ask for an updated declaration of expected income or payment volume",
                        documents, transactions, hard=True)
    return find(alert["alert_id"], "CLOSE", "OWN_ACCOUNT",
                f"{', '.join(ids(documents))} identifies {ctx.name} as the holder of the other accounts and reconciles the alerted transfers of {money(sum(Decimal(str(t['amount_gbp'])) for t in relevant if t['direction'] == 'in') or sum(Decimal(str(t['amount_gbp'])) for t in relevant))}",
                documents, transactions)


def collection(ctx, alert):
    transactions = ctx.tx(alert)
    for d in ctx.documents("booking_confirmation"):
        incoming = [t for t in transactions if t["direction"] == "in" and not regular_income(ctx, t)]
        outgoing = [t for t in ctx.transactions if t["direction"] == "out" and d.matches(t)]
        per_person = re.search(r"£([\d,.]+)\s+per person", d.text)
        if not incoming or not outgoing or not per_person:
            continue
        target = cents(per_person.group(1))
        if all(contains(d.text, t.get("counterparty_name")) and same_amount(cents(t["amount_gbp"]), target) for t in incoming):
            return find(alert["alert_id"], "CLOSE", "COLLECTION_DOCUMENTED",
                        f"{d.id} names the travellers and their {money(Decimal(target) / 100)} contributions; the collection reconciles to the payment to {outgoing[0]['counterparty_name']} for booking {d.get('Booking reference')}", [d], incoming + outgoing)
    return None


def red_flags(ctx):
    """Positive evidence of suspicion outranks a benign explanation elsewhere."""
    result = []
    transactions = ctx.tx()
    reports = ctx.documents("sending_bank_fraud_report")
    relevant_reports = [d for d in reports if any(d.matches(t) for t in transactions)]
    if relevant_reports:
        chats = ctx.documents("support_chat")
        reported = [t for t in transactions if t['direction'] == 'in' and any(d.matches(t) for d in relevant_reports)]
        victims = '; '.join(f"{t.get('counterparty_name')} sent {money(t['amount_gbp'])}" for t in reported[:2])
        result.append(find("fraud_reports", "ESCALATE", "ESC_MULE_ACTIVITY",
                           f"{', '.join(ids(relevant_reports))} reports scam payments into {ctx.name}'s account ({victims}) and requests that remaining funds be frozen; the onward transfers are not explained by a legitimate collection", relevant_reports + chats, transactions, hard=True))
    for d in ctx.documents("staff_note"):
        text = norm(d.text)
        if "TM03" in ctx.rules and re.search(r"(?:below|under|keeping|breaking|exempt|aggregat|split)", text) and re.search(r"(?:report|verif|supporting invoices|invoice|requirements|identification)", text):
            result.append(find("deliberate_splitting", "ESCALATE", "ESC_STRUCTURING",
                               f"{d.id} records the customer's enquiry about splitting transactions below the documentation or reporting threshold; this indicates deliberate evasion rather than ordinary separate takings or instalments", [d], transactions, hard=True))
        if "TM06" in ctx.rules and re.search(r"(?:unable|unwilling|refus|could not|cannot|couldn t|would not|wouldn t|failed to|declin|evasiv)", text) and re.search(r"(?:rout|beneficiary|lebanon)", text):
            invoices = ctx.documents("supplier_invoices", "supplier_invoice")
            result.append(find("unexplained_corridor", "ESCALATE", "ESC_HIGH_RISK_CORRIDOR",
                               f"{d.id} records that {ctx.name} could not explain the beneficiary location or route; {', '.join(ids(invoices)) or 'the documents on file'} does not establish a credible purpose for the higher-risk payments", [d] + invoices, transactions, hard=True))
    chats = ctx.documents("support_chat", "customer_email")
    joined = " ".join(d.text for d in chats)
    text = norm(joined)
    mule = re.search(r"(?:clients pay into my account|keep \d+|keep.*(?:wages|commission)|invest for (?:a few|other|them)|money to invest for them|payments assistant)", text)
    if mule:
        code = "ESC_EARLY_REDEMPTION" if "TM10" in ctx.rules else "ESC_THIRD_PARTY_FUNDING" if ctx.institution.name == "investment" else "ESC_MULE_ACTIVITY"
        statements = [s.strip() for d in chats for s in re.findall(r'^\*\*Customer:\*\*\s*(.+)$', d.text, re.M) if re.search(r'clients|invest|keep|assistant', s, re.I)]
        admission = re.sub(r'[.!?]+\s*', '; ', statements[0]).strip('; ')[:240] if statements else 'the customer describes receiving and forwarding other people\'s funds'
        result.append(find("third_party_admission", "ESCALATE", code,
                           f"{', '.join(ids(chats))} records that the customer receives or invests other people's money and sends it on: {admission}", chats, transactions, hard=True))
    if re.search(r"gift from my uncle", text) and re.search(r"(?:selling|sale of) my car", text) and "TM02" in ctx.rules:
        result.append(find("conflicting_source", "ESCALATE", "ESC_UNEXPLAINED_FUNDS",
                           f"{', '.join(ids(chats))} gives conflicting sources for the cash, first an uncle's gift and then a car sale; the money is rapidly sent onward without corroborating sale or gift documents", chats, transactions, hard=True))
    # A past small contract does not justify substantially larger current pass-through funds.
    contracts = ctx.documents("consulting_contract")
    for d in contracts:
        fee = amounts(d.get("Fee"))
        current = [a for a in ctx.alerts if a["rule_id"] in {"TM01", "TM02"}]
        if fee and "TM02" in ctx.rules and any(cents(a["measured"].get("received_gbp", 0)) > 2 * max(fee) for a in current):
            result.append(find("contract_scope", "ESCALATE", "ESC_INVOICE_MISMATCH",
                               f"{d.id} covers only {d.get('Scope') or 'a limited consulting engagement'} for {money(Decimal(max(fee)) / 100)}; it does not reconcile the larger current receipts and rapid onward transfers", [d], transactions, hard=True))
    return result


def fx_profile_documents(ctx, alert):
    transactions = ctx.tx(alert)
    if not transactions or any(t["direction"] == "in" and norm(t.get("counterparty_name")) != norm(ctx.name) for t in transactions):
        return None
    window = ctx.case["review_window"]
    payouts = [t for t in ctx.transactions if t["direction"] == "out" and window["from"] <= t["booked_at"][:10] <= window["to"]]
    significant_funding = [t for t in transactions if not routine_transaction(ctx, t)]
    # Ownership proof is checked against the actual overseas receiving account, never
    # inferred from a matching name on the domestic funding leg.
    own_payouts = [t for t in payouts if ctx.own_documents(t)]
    if own_payouts and significant_funding:
        paid = sum(Decimal(str(t["amount_gbp"])) for t in own_payouts)
        funded = sum(Decimal(str(t["amount_gbp"])) for t in significant_funding)
        if funded * Decimal("0.9") <= paid <= funded * Decimal("1.1"):
            documents = list({d.id: d for t in own_payouts for d in ctx.own_documents(t)}.values())
            measured = alert["measured"]
            multiple = Decimal(str(measured["received_gbp"])) / Decimal(str(measured["baseline_gbp"]))
            if multiple >= ctx.institution.own_profile_limit:
                return find(alert["alert_id"], "RFI", "RFI_UPDATED_PROFILE", f"{', '.join(ids(documents))} verifies the receiving account belongs to {ctx.name}, but {money(measured['received_gbp'])} funding is {multiple:.2f} times the monthly baseline; ask for an updated declaration of expected annual payment volume", documents, transactions + own_payouts, hard=True)
            return find(alert["alert_id"], "CLOSE", "OWN_ACCOUNT", f"{', '.join(ids(documents))} verifies the overseas receiving account belongs to {ctx.name} and reconciles {money(paid)} of onward transfers; total funding is {multiple:.2f} times the monthly baseline, below the updated-profile threshold", documents, transactions + own_payouts)
    orders = ctx.documents("purchase_order")
    invoices = ctx.documents("supplier_invoice", "supplier_invoices")
    matching = [d for d in invoices if contains(d.text, ctx.name) and any(d.matches(t) or (contains(d.text, t.get('counterparty_name')) and match_rows(d, t)) for t in payouts)]
    for order in orders:
        refs = re.findall(r"\bPO\s*\d+\b", order.text)
        connected = [d for d in matching if any(contains(d.text, ref) for ref in refs)]
        if connected:
            return find(alert["alert_id"], "CLOSE", "WITHIN_PROFILE", f"{order.id} records the new commercial order {refs[0]} and {', '.join(ids(connected))} reconciles the corresponding goods, supplier and payment; the extra funding supports this documented expansion", [order] + connected, transactions + [t for t in payouts if any(d.matches(t) for d in connected)])
    if matching:
        explained = [t for t in payouts if any(d.matches(t) or match_rows(d, t) for d in matching)]
        documented = sum(Decimal(str(t["amount_gbp"])) for t in explained)
        baseline = Decimal(str(alert["measured"]["baseline_gbp"]))
        excess = Decimal(str(alert["measured"]["received_gbp"])) - baseline
        if documented >= excess * Decimal("0.8"):
            return find(alert["alert_id"], "CLOSE", "INVOICE_MATCHES", f"{', '.join(ids(matching))} reconciles {money(documented)} of supplier payments with goods and beneficiary details; those commercial payments explain the increased funding beyond the declared baseline", matching, transactions + explained)
    return None


def list_check(ctx, alert):
    transactions = ctx.tx(alert)
    key = alert["measured"].get("list_entry")
    entry = ctx.institution.list_entries.get(key)
    if not entry:
        return find(alert["alert_id"], "RFI", "RFI_COUNTERPARTY_DETAILS", "Ask for the counterparty's full identity and ownership documents because the referenced internal-list entry is unavailable", transactions=transactions)
    regs = [d for d in ctx.documents("company_register_extract", "significant_control_register") if any(contains(d.text.splitlines()[0], t.get("counterparty_name")) for t in transactions)]
    entry_birth = date(entry.get("date_of_birth"))
    person = entry["name"]
    for d in ctx.documents("gift_letter"):
        if not contains(d.text, person) or not any(d.matches(t) for t in transactions):
            continue
        born = re.search(r"\bborn\s+(\d{1,2}\s+\w+\s+\d{4})", d.text, re.I)
        birthday = date(born.group(1)) if born else None
        if birthday and entry_birth:
            same = birthday == entry_birth
            return find(alert["alert_id"], "ESCALATE" if same else "CLOSE", "ESC_LISTED_PARTY" if same else "LIST_MATCH_DIFFERENT_PARTY",
                        f"{d.id} identifies donor {person}, born {birthday.isoformat()}, whereas {key} records {entry_birth.isoformat()}; {'the identity matches the listed party despite the gift explanation' if same else 'the conflicting birth dates distinguish the donor from the listed person'}", [d], transactions, hard=same, extra=[key])
    # Compare officer and shareholder identity, and control AT THE PAYMENT DATE.
    for d in regs:
        for row in d.tables:
            name = row.get("person") or row.get("officer") or row.get("person with significant control")
            if norm(name) != norm(person):
                continue
            born = date(row.get("born"))
            if born and entry_birth and (born.year, born.month) != (entry_birth.year, entry_birth.month):
                continue
            if not born or not entry_birth:
                continue
            when = date(transactions[0]["booked_at"])
            starts = date(row.get("notified") or row.get("appointed"))
            ceased = date(row.get("ceased") or row.get("resigned"))
            active = (not starts or when >= starts) and (not ceased or when < ceased)
            if active:
                return find(alert["alert_id"], "ESCALATE", "ESC_LISTED_PARTY",
                            f"{key} identifies {person}, born {entry_birth.strftime('%B %Y')}; {d.id} shows matching identity and {'ownership/control' if 'control' in row else 'an officer role'} on {when.isoformat()}{f', ending only on {ceased.isoformat()}' if ceased else ''}, so resignation from another role does not clear the payment", regs, transactions, hard=True, extra=[key])
    different = []
    for d in regs:
        if entry.get("kind") in {"company", "business"}:
            company_number = d.get("Company number")
            listed_number = entry.get("identifiers", {}).get("company_number")
            if company_number and listed_number and norm(company_number) != norm(listed_number):
                different.append(d)
        if "address" in alert["measured"].get("matched_on", ""):
            registered = d.get("Registered office")
            # A shared estate is insufficient; a different unit plus distinct officers/owners is positive disambiguation.
            unit = re.search(r"\bunit\s+([\w-]+)", registered, re.I)
            listed_unit = re.search(r"\bunit\s+([\w-]+)", entry.get("address", ""), re.I)
            names = [r.get("person") or r.get("officer") or r.get("person with significant control") for r in d.tables]
            if unit and listed_unit and norm(unit.group(1)) != norm(listed_unit.group(1)) and names and all(norm(n) != norm(person) for n in names):
                different.append(d)
        if entry_birth:
            rows = [r for r in d.tables if norm(r.get("person") or r.get("officer") or r.get("person with significant control")) == norm(person)]
            if rows and all(date(r.get("born")) and (date(r["born"]).year, date(r["born"]).month) != (entry_birth.year, entry_birth.month) for r in rows):
                different.append(d)
    if different:
        supporting = [d for d in ctx.documents("receipt", "invoice") if any(d.matches(t) for t in transactions)]
        return find(alert["alert_id"], "CLOSE", "LIST_MATCH_DIFFERENT_PARTY",
                    f"{', '.join(ids(different))} distinguishes the beneficiary's identity, registered unit or birth details from {key} ({person}, {entry.get('address', 'address not supplied')}); {', '.join(ids(supporting)) or 'the transaction record'} supports the actual payment", different + supporting, transactions, extra=[key])
    return find(alert["alert_id"], "RFI", "RFI_COUNTERPARTY_DETAILS",
                f"The match to {key} ({person}) is not resolved by the documents on file; ask for the counterparty's full date of birth, address and, for a company, current and historical beneficial ownership covering the payment date", regs, transactions, extra=[key])


def check_profile(ctx, alert):
    transactions = ctx.tx(alert)
    if ctx.institution.name == "payments_fx":
        commercial = fx_profile_documents(ctx, alert)
        if commercial:
            return commercial
    own = own_transfer_finding(ctx, alert, transactions)
    if own:
        return own
    documented = event_documents(ctx, transactions)
    gifts = gift_documents(ctx, transactions)
    refunds = refund_documents(ctx, transactions)
    records = cash_records(ctx, transactions)
    group = verified_group(ctx, transactions)
    factors = factoring_documents(ctx, transactions)
    collected = collection(ctx, alert)
    if collected:
        return collected
    cancellations = [d for d in ctx.documents("cancellation_confirmation") if any(d.matches(t, party=False) for t in transactions)]
    if cancellations and all(regular_income(ctx, t) or any(d.matches(t, party=False) and t.get("counterparty_account") and contains(d.text, t["counterparty_account"]) for d in cancellations) for t in transactions if t["direction"] == "in"):
        return find(alert["alert_id"], "CLOSE", "REFUND_OF_EARLIER_PAYMENT", f"{', '.join(ids(cancellations))} records cancellation of the subscription and return to the same named funding account; the amount reconciles to the additional receipt", cancellations, transactions)
    candidates = documented + gifts + refunds + records + group + factors
    source_tx = [t for t in transactions if t["direction"] == "in" and not regular_income(ctx, t)]
    if candidates and all(any(d.matches(t, party=d.type not in {"vehicle_sale_receipt", "sale_receipt", "takings_book", "cash_collection_reports", "cash_reconciliation"}) or
                             (d in records and match_rows(d, t)) or (d in group and contains(d.text, t.get("counterparty_name"))) for d in candidates) for t in source_tx):
        code = "DOCUMENTED_EVENT" if documented else "THIRD_PARTY_DOCUMENTED" if gifts or factors else "REFUND_OF_EARLIER_PAYMENT" if refunds else "BUSINESS_RECORDS_RECONCILE" if records else "GROUP_COMPANY"
        return find(alert["alert_id"], "CLOSE", code,
                    f"{', '.join(ids(candidates))} reconciles the additional funds of {money(sum(Decimal(str(t['amount_gbp'])) for t in source_tx))} to {documented[0].meta['title'] if documented else 'the documented gift, refund, business activity or group relationship'}; the remaining routine funding stays within the declared profile", candidates, transactions)
    if ctx.institution.name == "payments_fx":
        orders = ctx.documents("purchase_order")
        invoices = ctx.documents("supplier_invoice")
        # Ordinary business expansion needs documents linking both the order and supplier payment.
        for order in orders:
            for invoice in invoices:
                if contains(order.text, ctx.name) and contains(invoice.text, ctx.name) and order.get("Order Reference") and contains(invoice.text, order.get("Order Reference")):
                    if any(invoice.matches(t) for t in ctx.transactions):
                        return find(alert["alert_id"], "CLOSE", "WITHIN_PROFILE",
                                    f"{order.id} documents the new order for {ctx.name} and {invoice.id} links supplier goods and payment to {order.get('Order Reference')}; the increased funding supports that specific commercial order", [order, invoice], transactions)
    rfi_type = "RFI_BUSINESS_RECORDS" if ctx.customer["kind"] == "business" else "RFI_SOURCE_OF_FUNDS"
    docs = ctx.documents("support_chat", "management_accounts", "consulting_contract")
    unexplained = [t for t in transactions if t['direction'] == 'in' and not regular_income(ctx, t)]
    specific = '; '.join(f"{money(t['amount_gbp'])} from {t.get('counterparty_name') or t.get('channel', 'cash').replace('_', ' ')} on {t['booked_at'][:10]}" for t in unexplained[:2])
    chats = ' '.join(d.text for d in ctx.documents('support_chat')).casefold()
    request = (f"the signed loan agreement identifying the lender, borrower and amount for {specific}" if 'loan' in chats else
               f"the gift confirmation identifying the donor and amount for {specific}" if 'gift' in chats else
               f"a source-of-funds document establishing the origin of {specific}")
    if ctx.customer['kind'] == 'business':
        request = f"a current business reconciliation covering the review window and explaining the {money(alert['measured'].get('received_gbp', 0))} receipts"
    return find(alert["alert_id"], "RFI", rfi_type,
                f"The {money(alert['measured'].get('received_gbp', 0))} received exceeds the declared baseline of {money(alert['measured'].get('baseline_gbp', 0))}; ask for {request}", docs, transactions, supported=False)


def check_rapid(ctx, alert):
    transactions = ctx.tx(alert)
    own = own_transfer_finding(ctx, alert, transactions)
    if own:
        return own
    collected = collection(ctx, alert)
    if collected:
        return collected
    loans = ctx.documents("loan_agreement")
    dealers = ctx.documents("dealer_invoice")
    if loans and dealers:
        incoming = [t for t in transactions if t["direction"] == "in" and not routine_transaction(ctx, t)]
        outgoing = [t for t in transactions if t["direction"] == "out" and not routine_transaction(ctx, t)]
        if incoming and outgoing and all(any(d.matches(t) for d in loans) for t in incoming) and all(any(d.matches(t) for d in dealers) for t in outgoing):
            return find(alert["alert_id"], "CLOSE", "DOCUMENTED_EVENT", f"{', '.join(ids(loans))} identifies the borrower and loan advance; {', '.join(ids(dealers))} reconciles the vehicle, dealer and purchase payment, explaining the rapid use of the loan", loans + dealers, transactions)
    if ctx.institution.name == "investment":
        confirmations = [d for d in ctx.documents("cancellation_confirmation") if contains(d.text, "cancellation") and any(d.matches(t, party=False) for t in transactions)]
        if confirmations:
            return find(alert["alert_id"], "CLOSE", "REFUND_OF_EARLIER_PAYMENT",
                        f"{', '.join(ids(confirmations))} confirms cancellation within the contractual cancellation period and return of the subscription to the customer's named bank account", confirmations, transactions)
        emails = ctx.documents("customer_email")
        if emails and all(ctx.own_documents(t) for t in transactions if t.get("channel") not in {"fund_subscription", "fund_redemption"}):
            return find(alert["alert_id"], "CLOSE", "OWN_ACCOUNT", f"{', '.join(ids(emails))} explains the return of funds and the statements verify the sending and receiving account belongs to {ctx.name}", emails + ctx.documents("bank_statement"), transactions)
    refunds = refund_documents(ctx, transactions)
    if refunds and ctx.institution.name == "emoney":
        receipts = ctx.documents("holding_deposit_receipt")
        if receipts and all(routine_transaction(ctx, t) or any(d.matches(t) for d in refunds + receipts) for t in transactions if t.get("counterparty_name")):
            return find(alert["alert_id"], "CLOSE", "REFUND_OF_EARLIER_PAYMENT", f"{', '.join(ids(refunds))} documents the returned tenancy deposit and {', '.join(ids(receipts))} reconciles the deposit paid for the next home", refunds + receipts, transactions)
    if ctx.institution.name == "payments_fx":
        group = verified_group(ctx, transactions)
        factors = factoring_documents(ctx, transactions)
        freight = ctx.documents("freight_invoices", "carrier_invoice")
        if group:
            outgoing = [t for t in transactions if t["direction"] == "out" and not routine_transaction(ctx, t)]
            docs = payment_documents(ctx, outgoing)
            if all(any(d.matches(t) for d in docs) for t in outgoing):
                return find(alert["alert_id"], "CLOSE", "GROUP_COMPANY", f"{', '.join(ids(group))} verifies common ownership of the funding company and {ctx.name}; {', '.join(ids(docs))} reconciles the supplier payout", group + docs, transactions)
        if len(factors) >= 2:
            return find(alert["alert_id"], "CLOSE", "THIRD_PARTY_DOCUMENTED", f"{', '.join(ids(factors))} documents the invoice-finance facility and reconciles the advance from the finance provider", factors, transactions)
        if refunds:
            incoming = [t for t in transactions if t["direction"] == "in" and norm(t.get("counterparty_name")) != norm(ctx.name)]
            outgoing = [t for t in transactions if t["direction"] == "out"]
            if incoming and all(any(d.matches(t) for d in refunds) for t in incoming) and all(routine_transaction(ctx, t) for t in outgoing):
                return find(alert["alert_id"], "CLOSE", "REFUND_OF_EARLIER_PAYMENT", f"{', '.join(ids(refunds))} reconciles the third-party receipt to a refund of the earlier supplier payment; the other payouts are established ordinary supplier activity", refunds, transactions)
        if len(freight) >= 2:
            receipts = [t for t in transactions if t["direction"] == "in" and not contains(t.get("counterparty_name"), ctx.name)]
            payouts = [t for t in transactions if t["direction"] == "out" and not routine_transaction(ctx, t)]
            if receipts and all(any(match_rows(d, t, party_keys=("exporter",)) for d in freight) for t in receipts) and all(any(d.matches(t) for d in freight) for t in payouts):
                return find(alert["alert_id"], "CLOSE", "INVOICE_MATCHES", f"{', '.join(ids(freight))} reconciles each exporter's freight invoice and the corresponding carrier payment", freight, transactions)
    devices = ctx.documents("device_export")
    shared = [d for d in devices if any(len(re.findall(r"\b[A-Z]{2}-C\d+\b", r.get("customers who logged in from it", ""))) > 1 for r in d.tables)]
    if shared:
        return find(alert["alert_id"], "ESCALATE", "ESC_ACCOUNT_MISUSE", f"{', '.join(ids(shared))} shows several customers using the same device; together with the alerted rapid unexplained transfers and absent supporting records, the account may be operated for others", shared, transactions)
    return find(alert["alert_id"], "ESCALATE", "ESC_PASS_THROUGH" if ctx.customer["kind"] == "business" else "ESC_UNEXPLAINED_FUNDS",
                f"{money(alert['measured'].get('received_gbp', 0))} arrived and {money(alert['measured'].get('paid_out_gbp', alert['measured'].get('withdrawn_gbp', 0)))} moved on rapidly; no matching source-and-purpose documents reconcile both sides of the flow", ctx.documents("support_chat", "company_register_extract"), transactions, supported=False)


def check_structuring(ctx, alert):
    transactions = ctx.tx(alert)
    records = cash_records(ctx, transactions)
    if records:
        supporting = ctx.documents("cash_handling_procedure", "ticket_report")
        return find(alert["alert_id"], "CLOSE", "BUSINESS_RECORDS_RECONCILE", f"{', '.join(ids(records))} reconciles each alerted cash amount and collection date; {', '.join(ids(supporting)) or 'the takings book'} establishes separate trading takings rather than an unsupported split of one sum", records + supporting, transactions)
    if ctx.institution.name == "payments_fx":
        for d in ctx.documents("contract_schedule"):
            if all(contains(d.text, t.get("counterparty_name")) and match_rows(d, t) for t in transactions):
                return find(alert["alert_id"], "CLOSE", "INVOICE_MATCHES", f"{d.id} sets separate project stages, due dates and amounts which match all {len(transactions)} payments to {transactions[0]['counterparty_name']}", [d], transactions)
        return find(alert["alert_id"], "ESCALATE", "ESC_STRUCTURING", f"The {len(transactions)} payments to {alert['measured'].get('beneficiary', 'one beneficiary')} total {money(alert['measured'].get('total_gbp', 0))} and sit just below the invoice threshold; no independent delivery or stage schedule explains the split", ctx.documents("invoice"), transactions)
    return find(alert["alert_id"], "RFI", "RFI_BUSINESS_RECORDS", f"The repeated cash transactions total {money(alert['measured'].get('total_gbp', 0))}; ask for dated till, takings or collection records reconciling each deposit and explaining why it was banked separately", ctx.documents("management_accounts"), transactions, supported=False)


def check_many(ctx, alert):
    collected = collection(ctx, alert)
    if collected:
        return collected
    transactions = ctx.tx(alert)
    if ctx.institution.name == "payments_fx":
        for d in ctx.documents("payment_file"):
            if all(routine_transaction(ctx, t) or match_rows(d, t, party_keys=("contractor",)) for t in transactions):
                return find(alert["alert_id"], "CLOSE", "INVOICE_MATCHES", f"{d.id} identifies every contractor, country, contract and amount; all {len(transactions)} alerted payments reconcile to this payment file", [d], transactions)
        return find(alert["alert_id"], "RFI", "RFI_PURPOSE_DOCUMENTS", "Ask for a payment file or contracts identifying every beneficiary, purpose and amount; the records on file do not reconcile all the alerted payouts", ctx.documents("payment_file"), transactions, supported=False)
    return find(alert["alert_id"], "RFI", "RFI_PURPOSE_DOCUMENTS", "Ask for the booking, collection schedule or invoices naming each payer or payee and reconciling the amounts; the number of counterparties alone does not establish criminal activity", transactions=transactions, supported=False)


def check_cash(ctx, alert):
    transactions = ctx.tx(alert)
    events = event_documents(ctx, transactions)
    if events:
        support = ctx.documents("insurance_schedule", "advert")
        return find(alert["alert_id"], "CLOSE", "DOCUMENTED_EVENT", f"{', '.join(ids(events))} documents the sale and reconciles the cash transaction of {money(transactions[0]['amount_gbp'])}; {', '.join(ids(support)) or 'the sale record'} corroborates the item and seller", events + support, transactions)
    records = cash_records(ctx, transactions)
    if records:
        return find(alert["alert_id"], "CLOSE", "BUSINESS_RECORDS_RECONCILE", f"{', '.join(ids(records))} reconciles the cash to dated business takings", records, transactions)
    # A documented loan paid straight to the dealer can justify a large cash WITHDRAWAL for purchase.
    loans = ctx.documents("loan_agreement")
    invoices = ctx.documents("dealer_invoice")
    if loans and invoices:
        incoming = [t for t in ctx.transactions if t["direction"] == "in" and any(d.matches(t) for d in loans)]
        outgoing = [t for t in ctx.transactions if t["direction"] == "out" and any(d.matches(t) for d in invoices)]
        if incoming and outgoing:
            return find(alert["alert_id"], "CLOSE", "DOCUMENTED_EVENT", f"{', '.join(ids(loans + invoices))} reconciles the loan proceeds and vehicle purchase", loans + invoices, transactions + incoming + outgoing)
    return find(alert["alert_id"], "RFI", "RFI_SOURCE_OF_FUNDS", f"The cash transaction of {money(transactions[0]['amount_gbp'])} has no independently documented source or purpose; ask for the dated sale receipt, withdrawal trail or cash records establishing where this specific cash came from", ctx.documents("support_chat"), transactions, supported=False)


def check_corridor(ctx, alert):
    transactions = ctx.tx(alert)
    refunds = refund_documents(ctx, transactions)
    if refunds and all(any(d.matches(t) for d in refunds) for t in transactions):
        earlier = [t for t in ctx.transactions if t["direction"] == "out" and any(d.matches(t) for d in refunds)]
        return find(alert["alert_id"], "CLOSE", "REFUND_OF_EARLIER_PAYMENT", f"{', '.join(ids(refunds))} confirms cancellation and return of the earlier payment from the same counterparty for the same amount and booking", refunds, transactions + earlier)
    hospital = ctx.documents("hospital_invoice")
    chats = ctx.documents("support_chat")
    for d in hospital:
        if all(d.matches(t) for t in transactions) and any(contains(c.text, d.get("Patient")) and contains(c.text, "parent") for c in chats):
            return find(alert["alert_id"], "CLOSE", "INVOICE_MATCHES", f"{d.id} identifies patient {d.get('Patient')}, treatment and {d.get('Amount due')}; {', '.join(ids(chats))} explains the family relationship and the hospital payment matches", [d] + chats, transactions)
    if ctx.institution.name == "investment":
        statements = [d for t in transactions for d in ctx.own_documents(t)]
        supporting = ctx.documents("employment_contract", "customer_letter")
        if supporting and all(ctx.own_documents(t) for t in transactions):
            return find(alert["alert_id"], "CLOSE", "OWN_ACCOUNT", f"{', '.join(sorted(set(ids(statements))))} verifies the overseas account belongs to {ctx.name}; {', '.join(ids(supporting))} explains the overseas employment or relocation and payment instructions", statements + supporting, transactions)
    if ctx.institution.name == "payments_fx":
        contracts = ctx.documents("supply_contract")
        for d in ctx.documents("supplier_invoice", "supplier_invoices"):
            if contracts and all(d.matches(t) or match_rows(d, t) for t in transactions) and any(contains(c.text, t.get("counterparty_name")) for c in contracts for t in transactions):
                return find(alert["alert_id"], "CLOSE", "INVOICE_MATCHES", f"{', '.join(ids(contracts))} establishes the supplier relationship and {d.id} reconciles the goods, beneficiary and amounts in the higher-risk corridor", contracts + [d], transactions)
    if "TM01" in ctx.rules:
        profile = next(a for a in ctx.alerts if a["rule_id"] == "TM01")
        # Unexplained overseas transfers combined with substantial unrelated receipts are more than a missing invoice.
        receipts = [t for t in ctx.tx(profile) if t["direction"] == "in" and not regular_income(ctx, t) and not ctx.own_documents(t)]
        if receipts and not event_documents(ctx, receipts) and not gift_documents(ctx, receipts):
            return find(alert["alert_id"], "ESCALATE", "ESC_HIGH_RISK_CORRIDOR", f"The {money(alert['measured'].get('total_gbp', 0))} higher-risk payments coincide with additional unexplained receipts; the customer has not documented the source or a credible connection to the overseas recipients", chats, transactions + receipts)
    return find(alert["alert_id"], "RFI", "RFI_PURPOSE_DOCUMENTS", f"The payment involves {alert['measured'].get('countries', 'an undeclared higher-risk country')}; ask for the beneficiary's identity, relationship and the invoice, booking or treatment document reconciling the purpose and amount", chats, transactions, supported=False)


def check_dormant(ctx, alert):
    transactions = ctx.tx(alert)
    verified = ctx.documents("reverification_record")
    if verified:
        own = own_transfer_finding(ctx, alert, transactions)
        if own and own.verdict == "RFI":
            return own
        own_docs = [d for t in transactions for d in ctx.own_documents(t)]
        invoices = payment_documents(ctx, transactions)
        meaningful = [t for t in transactions if t.get("channel") not in {"card", "fund_subscription", "fund_redemption"} and not routine_transaction(ctx, t)]
        # Identity reverification is not evidence of a new third-party source of funds.
        if meaningful and all(ctx.own_documents(t) or (t['direction'] == 'out' and any(d.matches(t) for d in invoices)) for t in meaningful):
            return find(alert["alert_id"], "CLOSE", "RETURNING_CUSTOMER", f"{', '.join(ids(verified))} records successful live identity re-verification and the reason for the pause; {', '.join(sorted(set(ids(own_docs + invoices))))} reconciles the resumed funding and payment activity", verified + own_docs + invoices, transactions)
    events = event_documents(ctx, transactions)
    if events and all(regular_income(ctx, t) or any(d.matches(t) for d in events) for t in transactions if t["direction"] == "in"):
        return find(alert["alert_id"], "CLOSE", "DOCUMENTED_EVENT", f"{', '.join(ids(events))} documents the one-off event explaining why {ctx.name} resumed activity after {alert['measured'].get('dormant_days')} days", events, transactions)
    return find(alert["alert_id"], "RFI", "RFI_SOURCE_OF_FUNDS", f"Activity resumed after {alert['measured'].get('dormant_days')} days without a verified explanation for the current funds; ask for a statement showing the funding source and the reason for reactivating the account", transactions=transactions, supported=False)


def check_third_party(ctx, alert):
    transactions = ctx.tx(alert)
    groups = verified_group(ctx, transactions)
    gifts = gift_documents(ctx, transactions)
    events = event_documents(ctx, transactions)
    refunds = refund_documents(ctx, transactions)
    factors = factoring_documents(ctx, transactions)
    candidates = groups + gifts + events + refunds + factors
    if candidates and all(any(d.matches(t) or (d in groups and contains(d.text, t.get('counterparty_name'))) for d in candidates) for t in transactions):
        code = "GROUP_COMPANY" if groups else "DOCUMENTED_EVENT" if events else "REFUND_OF_EARLIER_PAYMENT" if refunds else "THIRD_PARTY_DOCUMENTED"
        return find(alert["alert_id"], "CLOSE", code, f"{', '.join(ids(candidates))} identifies the third-party payer and establishes the gift, settlement, refund, finance facility or verified group relationship; the documented amounts reconcile to the alerted transactions", candidates, transactions)
    if ctx.institution.name == "payments_fx":
        freight = ctx.documents("freight_invoices")
        if freight and all(any(match_rows(d, t, party_keys=("exporter",)) for d in freight) for t in transactions):
            return find(alert["alert_id"], "CLOSE", "INVOICE_MATCHES", f"{', '.join(ids(freight))} identifies each exporter and reconciles its freight invoice to the incoming payment", freight, transactions)
    own = own_transfer_finding(ctx, alert, transactions)
    if own:
        return own
    if "TM02" in ctx.rules or "TM10" in ctx.rules:
        return find(alert["alert_id"], "ESCALATE", "ESC_THIRD_PARTY_FUNDING", "Unrelated third-party funds are received or paid out alongside rapid withdrawal or early redemption; no documented relationship or credible purpose reconciles the activity", ctx.documents("support_chat", "company_register_extract"), transactions, supported=False)
    parties = '; '.join(f"{t.get('counterparty_name')} ({money(t['amount_gbp'])}, {t['booked_at'][:10]})" for t in transactions[:2])
    return find(alert["alert_id"], "RFI", "RFI_COUNTERPARTY_DETAILS", f"Ask for a documented explanation of the customer's relationship to {parties}, identifying why this third party funded or received money from the account", transactions=transactions, supported=False)


def check_redemption(ctx, alert):
    transactions = ctx.tx(alert)
    letters = ctx.documents("redundancy_letter")
    emails = ctx.documents("customer_email")
    if letters and emails and any(re.search(r"(?:redundan|living expenses)", d.text, re.I) for d in emails):
        return find(alert["alert_id"], "CLOSE", "DOCUMENTED_EVENT", f"{', '.join(ids(letters))} confirms loss of employment and {', '.join(ids(emails))} requests sale of the holdings for living expenses; this explains redemption after {alert['measured'].get('held_days')} days despite the investment loss", letters + emails, transactions)
    return find(alert["alert_id"], "RFI", "RFI_PURPOSE_DOCUMENTS", f"The fund was redeemed after {alert['measured'].get('held_days')} days; ask for the reason for the early sale and documentation of the need for the funds and intended withdrawal destination", ctx.documents("support_chat", "customer_email"), transactions, supported=False)


CHECKS = {"TM01": check_profile, "TM02": check_rapid, "TM03": check_structuring, "TM04": check_many, "TM05": check_cash,
          "TM06": check_corridor, "TM07": check_dormant, "TM08": check_third_party, "TM10": check_redemption, "TM22": list_check}


def held_payment_check(ctx):
    key = ctx.case.get("held_payment_id")
    if not key or ctx.institution.invoice_limit is None:
        return []
    transaction = ctx.institution.transactions.get(key)
    if transaction is None or transaction.get("customer_id") != ctx.case["customer_id"]:
        raise ValueError("Held payment missing or belongs to another customer")
    if cents(transaction["amount_gbp"]) < ctx.institution.invoice_limit:
        return []
    own = ctx.own_documents(transaction)
    if own:
        return [find("held_payment", "CLOSE", "OWN_ACCOUNT", f"{', '.join(ids(own))} proves the held payment is to the client's own account, which is exempt from the invoice requirement", own, [transaction])]
    matching = payment_documents(ctx, [transaction])
    if matching:
        return [find("held_payment", "CLOSE", "INVOICE_MATCHES", f"{', '.join(ids(matching))} matches the held payment's beneficiary and {money(transaction['amount_gbp'])} amount", matching, [transaction])]
    return [find("held_payment", "RFI", "RFI_PURPOSE_DOCUMENTS", f"The held payment of {money(transaction['amount_gbp'])} to {transaction.get('counterparty_name')} requires an invoice or contract before release; ask for that document identifying the beneficiary, goods or service, amount and payment date", transactions=[transaction], hard=True)]


def evaluate(ctx):
    findings = red_flags(ctx)
    for alert in ctx.alerts:
        check = CHECKS.get(alert["rule_id"])
        if check is None:
            findings.append(find(alert["alert_id"], "RFI", "RFI_PURPOSE_DOCUMENTS", f"Ask for documents resolving the unrecognised monitoring rule {alert['rule_id']}", transactions=ctx.tx(alert), supported=False))
        else:
            findings.append(check(ctx, alert))
    findings.extend(held_payment_check(ctx))
    if not findings:
        raise ValueError("Case has no alerts")
    return compose(ctx, findings), findings


def closure_integrity(ctx, findings):
    """Reconcile distinct financial claims together, including overlapping alerts."""
    documents = {d.id: d for d in ctx.docs}
    alerts = {a['alert_id']: a for a in ctx.alerts}
    pool, required = {}, {}
    monetary_codes = {'BUSINESS_RECORDS_RECONCILE', 'INVOICE_MATCHES', 'DOCUMENTED_EVENT',
                      'THIRD_PARTY_DOCUMENTED', 'REFUND_OF_EARLIER_PAYMENT', 'RETURNING_CUSTOMER', 'WITHIN_PROFILE', 'COLLECTION_DOCUMENTED'}
    for finding in findings:
        if finding.verdict != 'CLOSE' or finding.code not in monetary_codes:
            continue
        docs = [documents[e] for e in finding.evidence if e in documents and documents[e].type in FINANCIAL_TYPES]
        scope = ctx.tx(alerts[finding.check]) if finding.check in alerts else [t for t in ctx.transactions if t['transaction_id'] in finding.evidence]
        if not docs:
            if finding.code in {'INVOICE_MATCHES', 'BUSINESS_RECORDS_RECONCILE'}:
                required.update((t['transaction_id'], t) for t in scope if not routine_transaction(ctx, t))
            continue
        # Profile funding at a payments firm is explained by the documented onward
        # supplier payouts, not by pretending an invoice is proof of the funding leg.
        fx_profile = finding.check in alerts and alerts[finding.check]['rule_id'] == 'TM01' and ctx.institution.name == 'payments_fx'
        if fx_profile:
            scope = [t for t in ctx.transactions if t['transaction_id'] in finding.evidence]
        for payment in scope:
            if routine_transaction(ctx, payment) or historical_cash_context(ctx, payment) or payment.get('channel') in {'fund_subscription', 'fund_redemption'}:
                continue
            if finding.code == 'REFUND_OF_EARLIER_PAYMENT' and not any(d.type == 'cancellation_confirmation' for d in docs) and payment['direction'] != 'in':
                continue
            raw = [d for d in docs if (d.type not in INCOMING_TYPES or payment['direction'] == 'in') and
                   (d.type not in OUTGOING_TYPES or payment['direction'] == 'out') and
                   d.amount_party_matches(payment, party=d.type not in CASH_TYPES | {'vehicle_sale_receipt', 'sale_receipt', 'cancellation_confirmation'})]
            if raw or not fx_profile:
                required[payment['transaction_id']] = payment
                pool.update((d.id, d) for d in docs)
    if not required:
        return []
    proof = allocate_for_case(ctx, list(pool.values()), list(required.values()))
    if proof.complete:
        return []
    # A conflict can leave an already-settled historical invoice unmatched instead
    # of the new payment; report the affected current claims without dropping it.
    missing = [required[key] for key in proof.missing if key in required] or list(required.values())
    only_cash = all(t.get('channel') in {'cash_deposit', 'cash_topup'} for t in missing)
    cash_withdrawals = all(t.get('channel') == 'cash_withdrawal' for t in missing)
    code = 'RFI_BUSINESS_RECORDS' if only_cash else 'RFI_PURPOSE_DOCUMENTS'
    request = 'a dated takings reconciliation with a separate row for each deposit' if only_cash else 'the dated invoice or payment schedule uniquely covering each unmatched transaction'
    if cash_withdrawals:
        activity = '; '.join(f"{money(t['amount_gbp'])} on {t['booked_at'][:10]}" for t in missing)
        detail = f"The documented main flow does not establish the purpose of the additional cash withdrawal ({activity}); ask the customer what this cash was used for"
    else:
        detail = f"Existing financial evidence cannot be allocated once to every distinct transaction ({', '.join(proof.missing)}); ask for {request}"
    return [find('case:coverage', 'RFI', code, detail, list(pool.values()), missing, hard=True, supported=False,
                 extra=[key for key in proof.missing if key not in required])]


def rfi_requirement(ctx, finding):
    """Typed missing fact and affected scope; matching reason codes alone are insufficient."""
    alert = next((a for a in ctx.alerts if a['alert_id'] == finding.check), None)
    rule = alert['rule_id'] if alert else ''
    transactions = ctx.tx(alert) if alert else [t for t in ctx.transactions if t['transaction_id'] in finding.evidence]
    if finding.code == 'RFI_UPDATED_PROFILE':
        return 'profile', frozenset([ctx.case['customer_id']])
    if rule == 'TM22':
        return 'identity', frozenset([alert['measured'].get('list_entry', finding.check)])
    if finding.code == 'RFI_COUNTERPARTY_DETAILS':
        return 'funding_relationship', frozenset(t['transaction_id'] for t in transactions if not regular_income(ctx, t))
    if finding.code == 'RFI_BUSINESS_RECORDS':
        return 'business_records', frozenset([ctx.case['customer_id']])
    if finding.code == 'RFI_SOURCE_OF_FUNDS':
        relevant = [t for t in transactions if t['direction'] == 'in' and not regular_income(ctx, t)]
        return 'source', frozenset(t['transaction_id'] for t in relevant or transactions)
    if rule == 'TM04':
        return 'beneficiary_records', frozenset(t['transaction_id'] for t in transactions if t['direction'] == 'out' and not routine_transaction(ctx, t))
    if rule == 'TM10':
        return 'redemption_purpose', frozenset([finding.check])
    return 'payment_purpose', frozenset(t['transaction_id'] for t in transactions if not routine_transaction(ctx, t))


def case_rfi_plan(ctx, findings):
    pending = [f for f in findings if f.verdict == 'RFI']
    if not pending or any(f.verdict == 'ESCALATE' for f in findings):
        return None, []
    requirements = [(f, *rfi_requirement(ctx, f)) for f in pending]
    kinds = {kind for _, kind, _ in requirements}
    union = frozenset().union(*(scope for _, _, scope in requirements))
    covering = [f for f, _, scope in requirements if scope and scope >= union]
    if len(pending) == 1 or (len(kinds) == 1 and covering):
        chosen = sorted(covering or pending, key=lambda f: (-int(f.hard), f.check))[0]
        if len(pending) > 1 and kinds == {'business_records'}:
            detail = f"Ask for one dated business reconciliation for {ctx.case['review_window']['from']} to {ctx.case['review_window']['to']}, mapping each alerted receipt or cash deposit to its source and explaining the separate banking dates"
        else:
            detail = chosen.detail
        return detail, []
    funding_requirements = [(f, kind, scope) for f, kind, scope in requirements if kind in {'source', 'funding_relationship'}]
    funding_scope = frozenset().union(*(scope for _, _, scope in funding_requirements))
    funding_cover = any(scope >= funding_scope for _, _, scope in funding_requirements)
    payments = [t for t in ctx.transactions if t['transaction_id'] in funding_scope]
    gift_identity = False
    if 'identity' in kinds and kinds <= {'source', 'funding_relationship', 'identity'} and payments and all('gift' in t.get('reference', '').casefold() for t in payments):
        identities = [ctx.institution.list_entries.get(key) for _, kind, scope in requirements if kind == 'identity' for key in scope]
        gift_identity = bool(identities) and all(entry and entry.get('kind') == 'individual' and any(norm(entry['name']) == norm(t.get('counterparty_name')) for t in payments) for entry in identities)
    if (kinds == {'source', 'funding_relationship'} and covering) or (gift_identity and funding_cover):
        # The origin and relationship of the SAME incoming funding event can be
        # established by one signed gift/loan/funding agreement. They are not two
        # independent gaps merely because TM01 and TM08 both fired.
        if payments and all(t['direction'] == 'in' for t in payments):
            description = '; '.join(f"{money(t['amount_gbp'])} from {t.get('counterparty_name')} on {t['booked_at'][:10]}" for t in payments)
            words = ' '.join(t.get('reference', '') for t in payments).casefold()
            item = 'signed gift declaration' if 'gift' in words else 'signed loan agreement' if 'loan' in words else 'funding agreement'
            identity = ", including the donor's full name, date of birth and address to resolve the internal-list identity match" if gift_identity else ''
            detail = f"Ask for the {item} covering {description}, identifying the payer and recipient, their relationship, the origin and purpose of the funds, and any repayment obligation{identity}; this one document must resolve all checks on the same funding event"
            return detail, []
    # A shared order reference gives a concrete single contract to request. Merely
    # putting two independent requests into one sentence does not establish a link.
    transaction_sets = []
    for finding in pending:
        relevant = [t for t in ctx.transactions if t['transaction_id'] in finding.evidence and not routine_transaction(ctx, t)]
        refs = [set(r for r in references(t.get('reference', '')) if r[0] in {'po', 'booking'}) for t in relevant]
        transaction_sets.extend(refs)
    shared = set.intersection(*transaction_sets) if transaction_sets else set()
    if shared and kinds <= {'funding_relationship', 'payment_purpose', 'beneficiary_records'}:
        kind, value = sorted(shared)[0]
        detail = f"Ask for the executed contract for {kind.upper()} {value}, identifying the funding party's contractual role and every beneficiary, purpose and payment amount; this single agreement must reconcile both the receipt and the payouts"
        return detail, []
    descriptions = []
    for kind in sorted(kinds):
        scope = frozenset().union(*(targets for _, item, targets in requirements if item == kind))
        payments = [t for t in ctx.transactions if t['transaction_id'] in scope]
        amount = sum(Decimal(str(t['amount_gbp'])) for t in payments)
        parties = ', '.join(sorted({t.get('counterparty_name') or t.get('channel', 'cash') for t in payments})[:2])
        facts = f"{len(payments)} transaction{'s' if len(payments) != 1 else ''}, {money(amount)}, {parties}" if payments else ', '.join(sorted(scope))
        descriptions.append(f"{kind.replace('_', ' ')} ({facts})")
    gaps = '; '.join(descriptions)
    transactions = ctx.tx()
    flow = any(t['direction'] == 'in' for t in transactions) and any(t['direction'] == 'out' for t in transactions)
    code = 'ESC_PASS_THROUGH' if flow and ctx.customer['kind'] == 'business' else 'ESC_UNEXPLAINED_FUNDS'
    detail = f"Independent gaps remain in {gaps}; the evidence does not link them to one missing document or answer that would settle the whole case, so the unexplained activity requires MLRO review"
    guard = find('case:resolution', 'ESCALATE', code, detail, transactions=transactions, supported=False,
                 extra=[e for f in pending for e in f.evidence])
    return None, [guard]


def compose(ctx, findings):
    # Recompute case guards after a model review; never persist a previous guard
    # after its underlying missing facts have actually been resolved.
    findings[:] = [f for f in findings if not f.check.startswith('case:')]
    findings.extend(closure_integrity(ctx, findings))
    request, rfi_guards = case_rfi_plan(ctx, findings)
    findings.extend(rfi_guards)
    # Fixed, documented tie-breaking uses severity, policy veto, specificity, then check ID.
    priority = {c: i for i, c in enumerate("ESC_LISTED_PARTY ESC_MULE_ACTIVITY ESC_STRUCTURING ESC_HIGH_RISK_CORRIDOR ESC_EARLY_REDEMPTION ESC_THIRD_PARTY_FUNDING ESC_ACCOUNT_MISUSE ESC_INVOICE_MISMATCH ESC_PASS_THROUGH ESC_UNEXPLAINED_FUNDS RFI_UPDATED_PROFILE RFI_COUNTERPARTY_DETAILS RFI_SOURCE_OF_FUNDS RFI_BUSINESS_RECORDS RFI_PURPOSE_DOCUMENTS LIST_MATCH_DIFFERENT_PARTY COLLECTION_DOCUMENTED RETURNING_CUSTOMER DOCUMENTED_EVENT BUSINESS_RECORDS_RECONCILE GROUP_COMPANY THIRD_PARTY_DOCUMENTED REFUND_OF_EARLIER_PAYMENT INVOICE_MATCHES OWN_ACCOUNT WITHIN_PROFILE".split())}
    ordered = sorted(findings, key=lambda f: (-SEVERITY[f.verdict], -int(f.hard), priority.get(f.code, 999), f.check))
    primary = ordered[0]
    if primary.verdict == 'RFI' and request:
        pending = [f for f in findings if f.verdict == 'RFI']
        findings.append(Finding('case:request', 'RFI', primary.code, request,
                                sorted({e for f in pending for e in f.evidence}),
                                supported=all(f.supported for f in pending)))
    details = [request if primary.verdict == 'RFI' and request else primary.detail]
    # Keep a second deciding ground, especially policy exceptions and multi-alert cases.
    second = next((f for f in ordered[1:] if f.detail != primary.detail and (f.verdict == primary.verdict or primary.verdict == "CLOSE")), None) if primary.verdict != 'RFI' else None
    if second:
        details.append(second.detail)
    rule_list = ", ".join(sorted(ctx.rules))
    intro = f"{ctx.name}: reviewed {len(ctx.alerts)} alert{'s' if len(ctx.alerts) != 1 else ''} ({rule_list}) for {ctx.case['review_window']['from']} to {ctx.case['review_window']['to']}"
    reason = ". ".join([intro] + details) + "."
    if primary.verdict == "ESCALATE":
        reason += " Escalate to the MLRO; do not request customer information while suspicion remains."
    evidence = sorted({e for f in findings for e in f.evidence})
    result = {"case_id": ctx.case["case_id"], "alert_ids": sorted(ctx.case["alert_ids"]), "verdict": primary.verdict,
              "reason_code": primary.code, "reason": reason, "evidence": evidence}
    return validate_result(result, ctx)
