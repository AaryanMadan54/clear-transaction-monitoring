"""Dated financial evidence and deterministic one-to-one transaction allocation.

The date windows below are conservative reconciliation heuristics, not bank policy.
An explicit due date, payment term or matching payment reference can prove
the link for an older document. An explicit contradictory reference is never ignored.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass

from domain import amounts, cents, contains, date, norm, same_amount

CASH_TYPES = {'takings_book', 'cash_collection_reports', 'cash_reconciliation'}
ROW_TYPES = CASH_TYPES | {'contract_schedule', 'supplier_invoices', 'freight_invoices', 'payment_file'}
FINANCIAL_TYPES = ROW_TYPES | {'invoice', 'supplier_invoice', 'hospital_invoice', 'dealer_invoice', 'consulting_contract', 'carrier_invoice',
    'insurer_letter', 'solicitor_letter', 'completion_statement', 'settlement_letter', 'vehicle_sale_receipt', 'sale_receipt',
    'loan_agreement', 'gift_letter', 'venue_email', 'credit_note', 'deposit_return_letter', 'holding_deposit_receipt',
    'remittance_advice', 'cancellation_confirmation', 'booking_confirmation'}
OUTGOING_TYPES = {'invoice', 'supplier_invoice', 'supplier_invoices', 'contract_schedule',
                  'hospital_invoice', 'dealer_invoice', 'carrier_invoice', 'holding_deposit_receipt'}
INCOMING_TYPES = {'insurer_letter', 'solicitor_letter', 'completion_statement', 'settlement_letter', 'vehicle_sale_receipt',
    'sale_receipt', 'loan_agreement', 'gift_letter', 'venue_email', 'credit_note', 'deposit_return_letter', 'remittance_advice'}
MONTHS = '|'.join(calendar.month_name[1:])


def references(text):
    """Invoice/order/payment IDs, never document metadata IDs or arbitrary numbers."""
    text = re.sub(r'\bInvoice\s+(?=INV(?:\s|[-/#\d]))', '', text or '', flags=re.I)
    result = set()
    for match in re.finditer(r'\b(INV(?:OICE)?|PO|BOOKING|CARRIER|FREIGHT)\.?\s*(?:NO\.?\s*)?[#:\s/-]*([A-Z0-9][A-Z0-9/-]*)', text, re.I):
        kind, value = match.groups()
        if not any(c.isdigit() for c in value) and not any(c in value for c in '/-') and not re.search(r'\bINV-', match.group(0), re.I):
            continue
        kind = 'invoice' if kind.lower().startswith('inv') else kind.lower()
        result.add((kind, re.sub(r'[^a-z0-9]', '', value.casefold())))
    # Named invoice references can omit the INV prefix (e.g. "Invoice AB/123").
    return result


def document_references(doc, row=None):
    if row is not None:
        values = [v for k, v in row.items() if k in {'invoice', 'reference', 'payment reference', 'order', 'order reference', 'contract'}]
        return references(' '.join(values))
    lines = [line for line in doc.text.splitlines() if not re.match(r'\*\*(?:Document ID|Dated):', line, re.I)]
    result = references('\n'.join(lines))
    for key, kind in [('Booking reference', 'booking'), ('Invoice number', 'invoice'), ('Invoice reference', 'invoice')]:
        value = doc.get(key)
        if value:
            result.add((kind, re.sub(r'[^a-z0-9]', '', value.casefold())))
    return result


def reference_link(doc, transaction, row=None):
    actual = references(transaction.get('reference', ''))
    expected = document_references(doc, row)
    if row is not None:
        row_kinds = {kind for kind, _ in expected}
        expected |= {ref for ref in document_references(doc) if ref[0] not in row_kinds}
    # Compare only like identifiers: an invoice can legitimately also name a PO.
    for kind in {k for k, _ in actual} & {k for k, _ in expected}:
        if not ({v for k, v in actual if k == kind} & {v for k, v in expected if k == kind}):
            return False
    if actual & expected:
        return True
    # Invoice headings may be "Invoice AB/123" and the transfer simply "AB/123".
    ref = norm(transaction.get('reference'))
    if ref and ref not in {'transfer', 'payment', 'funding', 'cash deposit', 'cash top up'} and any(v and re.sub(r'[^a-z0-9]', '', ref) == v for _, v in expected):
        return True
    return None


def document_link(doc, transaction, row=None):
    linked = reference_link(doc, transaction, row)
    if linked is False:
        return False
    booked = date(transaction.get('booked_at'))
    if not booked:
        return False
    if row is not None:
        # A row's explicit accounting date outranks the date the report was printed.
        raw = next((row[k] for k in ['date', 'due', 'banked', 'emptied', 'payment date'] if row.get(k)), None)
        if raw is not None:
            when = date(raw)
            return bool(when and 0 <= (booked - when).days <= 7)
    for key in ['due date', 'payment date', 'payment due', 'due']:
        if doc.get(key):
            due = date(doc.get(key))
            return bool(due and abs((booked - due).days) <= 7)
    # Explicitly dated transaction entries can corroborate an older statement/report.
    lines = [line for line in doc.text.splitlines() if not re.match(r'\*\*(?:Document ID|Dated):', line, re.I)]
    for line in lines:
        if any(same_amount(v, cents(transaction['amount_gbp'])) for v in amounts(line)):
            for raw in re.findall(r'\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}\s+(?:' + MONTHS + r')\s+\d{4})\b', line, re.I):
                when = date(raw)
                if when and abs((booked - when).days) <= 7:
                    return True
    if linked:
        return True
    issued = date(doc.get('Dated') or doc.meta.get('dated'))
    # Without an identifying reference, require contemporary evidence. Older
    # contractual payment terms can extend the period, rather than assuming expiry.
    terms = re.search(r'\bnet\s+(\d+)\b|within\s+(\d+)\s+days', doc.get('Terms'), re.I)
    window = int(next(v for v in terms.groups() if v)) if terms else 31
    return bool(issued and -7 <= (booked - issued).days <= window)


def row_matches(doc, row, transaction, amount_keys=None, party_keys=()):
    if amount_keys is None:
        amount_keys = ('amount', 'total', 'cash topped up', 'cash counted', 'amount each', 'per payment', 'each amount')
    each_keys = tuple(k for k in ['amount each', 'per payment', 'each amount'] if row.get(k))
    if each_keys:
        amount_keys = each_keys
    if not any(same_amount(v, cents(transaction['amount_gbp'])) for k, text in row.items() if k in amount_keys for v in amounts(text)):
        return False
    keys = party_keys or tuple(k for k in ['contractor', 'payee', 'exporter', 'beneficiary', 'supplier'] if k in row)
    if keys and not any(norm(row.get(k)) == norm(transaction.get('counterparty_name')) for k in keys):
        return False
    if not keys and doc.type not in CASH_TYPES and not contains(doc.text, transaction.get('counterparty_name')):
        return False
    return document_link(doc, transaction, row)


@dataclass(frozen=True)
class Unit:
    doc_id: str
    index: int
    slot: int


@dataclass
class Allocation:
    assignments: dict
    missing: tuple

    @property
    def complete(self):
        return not self.missing


def allocate(documents, transactions, rows_only=False):
    """Maximum bipartite matching; a row/document slot funds one distinct payment.

    Repeated alert references to one transaction are deduplicated. Consumption is
    local to this proof, never retained between cases or evaluation calls.
    """
    docs = {d.id: d for d in documents}
    txs = {t['transaction_id']: t for t in transactions}
    edges = {key: [] for key in sorted(txs)}
    for key, payment in sorted(txs.items()):
        for doc_id, doc in sorted(docs.items()):
            if doc.type in OUTGOING_TYPES and payment['direction'] != 'out':
                continue
            if doc.type in INCOMING_TYPES and payment['direction'] != 'in':
                continue
            if doc.type == 'booking_confirmation' and not rows_only:
                each = re.search(r'£([\d,.]+)\s+per person', doc.text)
                total = re.search(r'£([\d,.]+)\s+in total', doc.text)
                travellers = re.findall(r'^\d+\.\s*(.+?)\s*$', doc.text, re.M)
                if not document_link(doc, payment):
                    continue
                if payment['direction'] == 'in' and each and same_amount(cents(each.group(1)), cents(payment['amount_gbp'])):
                    edges[key].extend(Unit(doc_id, index, 0) for index, person in enumerate(travellers)
                                      if norm(person) == norm(payment.get('counterparty_name')))
                elif payment['direction'] == 'out' and total and same_amount(cents(total.group(1)), cents(payment['amount_gbp'])) and doc.amount_party_matches(payment):
                    edges[key].append(Unit(doc_id, -1, 0))
            elif doc.type in ROW_TYPES or rows_only:
                for index, row in enumerate(doc.tables):
                    slots = 1
                    raw_count = next((row[k] for k in ['payments', 'transactions', 'deposits', 'number of payments', 'number of deposits'] if k in row), '')
                    if not raw_count and row.get('per payment'):
                        raw_count = row.get('count', '')
                    explicit_each = next((row[k] for k in ['amount each', 'per payment', 'each amount'] if k in row), '')
                    if re.fullmatch(r'\d+', raw_count.strip()) and 1 <= int(raw_count) <= 1000 and explicit_each:
                        slots = int(raw_count)
                    if row_matches(doc, row, payment):
                        edges[key].extend(Unit(doc_id, index, slot) for slot in range(slots))
            elif doc.matches(payment, party=doc.type not in {'vehicle_sale_receipt', 'sale_receipt', 'cancellation_confirmation'}):
                # A cancellation confirmation explicitly covers the original
                # subscription and its return, one transaction in each direction.
                index = -2 if doc.type == 'cancellation_confirmation' and payment['direction'] == 'out' else -1
                edges[key].append(Unit(doc_id, index, 0))
    occupied = {}

    def assign(key, visited):
        for unit in edges[key]:
            if unit in visited:
                continue
            visited.add(unit)
            if unit not in occupied or assign(occupied[unit], visited):
                occupied[unit] = key
                return True
        return False

    for key in sorted(txs, key=lambda k: (len(edges[k]), k)):
        assign(key, set())
    assignments = {key: unit for unit, key in occupied.items()}
    return Allocation(assignments, tuple(sorted(set(txs) - set(assignments))))


def allocate_for_case(ctx, documents, transactions, rows_only=False):
    """Reserve units demonstrably used by earlier payments of the same invoice.

    Only an explicit matching reference reserves historical evidence. A coincidentally
    equal routine payment to the same supplier does not establish prior settlement.
    """
    transactions = list(transactions)
    if not transactions:
        return allocate(documents, [], rows_only)
    wanted = {t['transaction_id'] for t in transactions}
    earliest = min(t['booked_at'] for t in transactions)
    prior = [t for t in ctx.transactions if t['transaction_id'] not in wanted and t['booked_at'] < earliest and
             any(reference_link(d, t) is True and allocate([d], [t], rows_only).complete for d in documents)]
    return allocate(documents, transactions + prior, rows_only)
