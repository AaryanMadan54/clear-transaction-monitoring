"""Policy boundary and adversarial tests. All fixtures are synthetic, never development answers."""
from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from domain import Context, Document, Finding, Institution, amounts, validate_result
from engine import compose, evaluate
from model import ModelReview, apply_review, validate_findings


def tx(key="TX-A", amount=12000, direction="in", party="Example Person", account="EXAMPLE-ACCOUNT", **extra):
    return dict(transaction_id=key, customer_id="CUSTOMER-A", account_id="ACCOUNT-A", booked_at="2026-09-15T10:00:00Z",
                direction=direction, channel="faster_payment", amount_gbp=amount, counterparty_name=party,
                counterparty_account=account, counterparty_country="United Kingdom", reference="TRANSFER", **extra)


def doc(key, kind, body):
    return Document({"doc_id": key, "customer_id": "CUSTOMER-A", "type": kind, "title": kind.replace("_", " "), "dated": "2026-09-16", "file": "evidence/" + key + ".md"}, body)


def context(rules=("TM01",), documents=(), transactions=None, name="bank", baseline=3000, received=12000, held=None):
    transactions = transactions or [tx()]
    inst = SimpleNamespace(name=name, own_profile_limit=Decimal(5), invoice_limit=1000000 if name == "payments_fx" else None,
                           transactions={t["transaction_id"]: t for t in transactions}, list_entries={})
    customer = {"customer_id": "CUSTOMER-A", "kind": "business" if name == "payments_fx" else "individual", "name": "Example Person", "employer": "Example Employer",
                "declared": {"monthly_income_gbp": baseline, "expected_countries": ["United Kingdom"]}}
    alerts = []
    for i, rule in enumerate(rules):
        measured = {"received_gbp": received, "baseline_gbp": baseline, "multiple": received / baseline, "total_gbp": received,
                    "paid_out_gbp": received, "dormant_days": 200, "held_days": 30}
        alerts.append({"alert_id": f"ALERT-{i}", "case_id": "CASE-A", "customer_id": "CUSTOMER-A", "rule_id": rule, "measured": measured,
                       "transaction_ids": [t["transaction_id"] for t in transactions], "message": "Synthetic activity"})
    case = {"case_id": "CASE-A", "customer_id": "CUSTOMER-A", "review_window": {"from": "2026-09-01", "to": "2026-09-30"},
            "alert_ids": [a["alert_id"] for a in alerts], "held_payment_id": held}
    return Context(inst, case, customer, alerts, list(documents), transactions, [])


def statement(holder="Example Person", account="EXAMPLE-ACCOUNT", amount=12000):
    return doc("DOC-STATEMENT", "bank_statement", f"# Account statement\n\n**Account holder:** {holder}\n**Account:** {account}\n\n15 September 2026: transfer of £{amount:,.2f}.")


class PolicyTests(unittest.TestCase):
    def test_currency_parser_does_not_treat_dates_as_money(self):
        self.assertEqual(amounts("15 September 2026: £12,345.67; reference 2026; GBP 15.00"), [1234567, 1500])

    def test_own_account_below_five_times_closes(self):
        result, _ = evaluate(context(documents=[statement()]))
        self.assertEqual((result["verdict"], result["reason_code"]), ("CLOSE", "OWN_ACCOUNT"))

    def test_exactly_five_times_needs_updated_profile(self):
        result, findings = evaluate(context(documents=[statement(amount=15000)], transactions=[tx(amount=15000)], received=15000))
        self.assertEqual(result["reason_code"], "RFI_UPDATED_PROFILE")
        self.assertTrue(any(f.hard for f in findings))

    def test_five_times_uses_unrounded_values(self):
        result, _ = evaluate(context(documents=[statement(amount=14999.99)], transactions=[tx(amount=14999.99)], received=14999.99))
        self.assertEqual(result["verdict"], "CLOSE")

    def test_matching_name_without_document_is_not_ownership_proof(self):
        result, _ = evaluate(context())
        self.assertEqual(result["verdict"], "RFI")

    def test_other_persons_statement_does_not_prove_ownership(self):
        result, _ = evaluate(context(documents=[statement(holder="Someone Else")]))
        self.assertEqual(result["verdict"], "RFI")

    def test_ownership_proof_does_not_hide_unexplained_cash(self):
        cash = tx(key="TX-CASH", amount=4000, party=None, account=None)
        cash['channel'] = 'cash_deposit'
        result, _ = evaluate(context(documents=[statement()], transactions=[tx(), cash], received=16000))
        self.assertNotEqual(result['verdict'], 'CLOSE')

    def test_different_account_does_not_prove_ownership(self):
        result, _ = evaluate(context(documents=[statement(account="UNRELATED")], transactions=[tx(party="Other Sender")]))
        self.assertEqual(result["verdict"], "RFI")

    def test_joint_holder_is_recognised(self):
        joint = doc("DOC-JOINT", "bank_statement", "**Account holders:** Example Person and Example Partner\n**Account:** EXAMPLE-ACCOUNT\n£12,000.00")
        result, _ = evaluate(context(documents=[joint]))
        self.assertEqual(result["verdict"], "CLOSE")

    def test_unexplained_rapid_flow_escalates(self):
        result, _ = evaluate(context(rules=["TM02"]))
        self.assertEqual(result["verdict"], "ESCALATE")

    def test_one_explained_alert_does_not_close_entire_case(self):
        result, _ = evaluate(context(rules=["TM01", "TM10"], documents=[statement()]))
        self.assertEqual(result["verdict"], "RFI")
        self.assertEqual(result["reason_code"], "RFI_PURPOSE_DOCUMENTS")

    def test_inheritance_reconciles_client_account_suffix(self):
        letter = doc("DOC-ESTATE", "solicitor_letter", "# Letter from Example Solicitors LLP\nYour share of the estate is £12,000.00 and will be paid to your account.")
        result, _ = evaluate(context(documents=[letter], transactions=[tx(party="Example Solicitors LLP client account")]))
        self.assertEqual(result["reason_code"], "DOCUMENTED_EVENT")

    def test_gift_does_not_trigger_own_account_five_times_rule(self):
        letter = doc("DOC-GIFT", "gift_letter", "I, Example Donor, gift £24,000.00 to my child Example Person. This is a non-repayable gift and I retain no interest.")
        result, _ = evaluate(context(rules=["TM01", "TM08"], documents=[letter], transactions=[tx(amount=24000, party="Example Donor")], received=24000))
        self.assertEqual(result["verdict"], "CLOSE")

    def listed_context(self, birthday="1960-02-12", donor_birthday="12 February 1960"):
        letter = doc("DOC-GIFT", "gift_letter", f"I, Example Donor, born {donor_birthday}, gift £12,000.00 to Example Person. No repayment is required and I hold no interest.")
        ctx = context(rules=["TM22"], documents=[letter], transactions=[tx(party="Example Donor")])
        ctx.institution.list_entries["LIST-A"] = {"entry_id": "LIST-A", "kind": "individual", "name": "Example Donor", "date_of_birth": birthday}
        ctx.alerts[0]["measured"].update(list_entry="LIST-A", matched_on="name")
        return ctx

    def test_identical_donor_birthday_escalates(self):
        result, _ = evaluate(self.listed_context())
        self.assertEqual(result["reason_code"], "ESC_LISTED_PARTY")

    def test_different_donor_birthday_closes_list_match(self):
        result, _ = evaluate(self.listed_context(donor_birthday="12 February 1990"))
        self.assertEqual(result["reason_code"], "LIST_MATCH_DIFFERENT_PARTY")

    def test_name_only_list_match_requires_information(self):
        ctx = self.listed_context()
        ctx.docs = []
        result, _ = evaluate(ctx)
        self.assertEqual(result["reason_code"], "RFI_COUNTERPARTY_DETAILS")

    def test_resigned_director_with_ongoing_share_control_escalates(self):
        register = doc("DOC-PSC", "significant_control_register", "# Control register for Example Recipient Ltd\n\n| Person | Born | Control | Notified | Ceased |\n|---|---|---|---|---|\n| Example Donor | February 1960 | Owns 60% of shares | 1 January 2020 | 16 September 2026 |")
        ctx = self.listed_context()
        ctx.transactions[0]["counterparty_name"] = "Example Recipient Ltd"
        ctx.docs = [register]
        result, _ = evaluate(ctx)
        self.assertEqual(result["reason_code"], "ESC_LISTED_PARTY")

    def test_expired_control_does_not_count_as_current_control(self):
        register = doc("DOC-PSC", "significant_control_register", "# Control register for Example Recipient Ltd\n\n| Person | Born | Control | Notified | Ceased |\n|---|---|---|---|---|\n| Example Donor | February 1960 | Owns 60% of shares | 1 January 2020 | 14 September 2026 |")
        ctx = self.listed_context()
        ctx.transactions[0]["counterparty_name"] = "Example Recipient Ltd"
        ctx.docs = [register]
        result, _ = evaluate(ctx)
        self.assertNotEqual(result["verdict"], "ESCALATE")

    def test_invoice_required_at_exactly_ten_thousand(self):
        payment = tx(amount=10000, direction="out", party="Example Supplier")
        ctx = context(rules=["TM07"], transactions=[payment], name="payments_fx", held="TX-A")
        result, fs = evaluate(ctx)
        self.assertTrue(any(f.check == "held_payment" and f.verdict == "RFI" for f in fs))
        self.assertNotEqual(result["verdict"], "CLOSE")

    def test_invoice_not_required_below_ten_thousand(self):
        ctx = context(rules=["TM07"], transactions=[tx(amount=9999.99, direction="out")], name="payments_fx", held="TX-A")
        _, fs = evaluate(ctx)
        self.assertFalse(any(f.check == "held_payment" for f in fs))

    def test_wrong_beneficiary_invoice_is_rejected(self):
        invoice = doc("DOC-INVOICE", "supplier_invoice", "**Supplier:** Unrelated Business\n**Amount:** £10,000.00")
        ctx = context(rules=["TM07"], transactions=[tx(amount=10000, direction="out", party="Example Supplier")], documents=[invoice], name="payments_fx", held="TX-A")
        _, fs = evaluate(ctx)
        self.assertTrue(any(f.check == "held_payment" and f.verdict == "RFI" for f in fs))

    def test_evasion_note_outranks_reconciling_invoice(self):
        note = doc("DOC-NOTE", "staff_note", "Customer asked whether splitting payments below £10,000 would avoid supporting invoices.")
        ctx = context(rules=["TM03"], documents=[note], name="payments_fx")
        result, _ = evaluate(ctx)
        self.assertEqual(result["reason_code"], "ESC_STRUCTURING")

    def test_shared_device_alone_is_not_an_escalation_trigger(self):
        device = doc("DOC-DEVICE", "device_export", "| Device | Customers who logged in from it |\n|---|---|\n| SHARED | AA-C123, AA-C456 |")
        result, _ = evaluate(context(documents=[device]))
        self.assertEqual(result["verdict"], "RFI")

    def test_invalid_evidence_id_is_rejected(self):
        ctx = context(documents=[statement()])
        result, _ = evaluate(ctx)
        result["evidence"].append("DOES-NOT-EXIST")
        with self.assertRaises(ValueError):
            validate_result(result, ctx)

    def test_alert_omission_is_rejected(self):
        ctx = context(documents=[statement()])
        result, _ = evaluate(ctx)
        result["alert_ids"] = []
        with self.assertRaises(ValueError):
            validate_result(result, ctx)

    def test_reordered_documents_have_identical_output(self):
        ctx = context(documents=[statement(), doc("DOC-OTHER", "contact_note", "Routine phone number update")])
        first, _ = evaluate(ctx)
        ctx.docs.reverse()
        second, _ = evaluate(ctx)
        self.assertEqual(first, second)

    def test_fresh_repeated_evaluation_is_identical(self):
        outputs = [evaluate(context(documents=[statement()]))[0] for _ in range(5)]
        self.assertTrue(all(o == outputs[0] for o in outputs))


class ModelBoundaryTests(unittest.TestCase):
    def candidate(self, ctx):
        d = ctx.docs[0]
        return {"findings": [{"alert_id": "ALERT-0", "verdict": "CLOSE", "reason_code": "OWN_ACCOUNT",
                              "detail": "The statement identifies the customer's account and reconciles the transfer.",
                              "evidence": [d.id, "TX-A"], "quotes": [{"doc_id": d.id, "text": "**Account holder:** Example Person"}]}]}

    def test_hallucinated_quote_rejected(self):
        ctx = context(documents=[statement()])
        payload = self.candidate(ctx)
        payload["findings"][0]["quotes"][0]["text"] = "Invented documentary proof that does not exist"
        with self.assertRaises(ValueError):
            validate_findings(payload, ctx)

    def test_closure_without_documentary_quote_rejected(self):
        ctx = context(documents=[statement()])
        payload = self.candidate(ctx)
        payload["findings"][0]["quotes"] = []
        with self.assertRaises(ValueError):
            validate_findings(payload, ctx)

    def test_model_cannot_override_five_times_policy(self):
        ctx = context(documents=[statement(amount=15000)], transactions=[tx(amount=15000)], received=15000)
        baseline, fs = evaluate(ctx)
        payload = self.candidate(ctx)
        clean = validate_findings(payload, ctx)
        result, _ = apply_review(ctx, baseline, fs, {"status": "reviewed", "findings": [f.as_dict() for f in clean]})
        self.assertEqual(result["reason_code"], "RFI_UPDATED_PROFILE")

    def test_unavailable_model_retains_baseline(self):
        ctx = context()
        baseline, fs = evaluate(ctx)
        self.assertEqual(apply_review(ctx, baseline, fs, {"status": "failed"})[0], baseline)

    def test_no_key_does_not_make_a_request(self):
        with patch.dict("os.environ", {}, clear=True), patch("urllib.request.urlopen") as network:
            review = ModelReview()
            ctx = context()
            baseline, fs = evaluate(ctx)
            self.assertEqual(review.review(ctx, baseline, fs)["status"], "unavailable")
            network.assert_not_called()

    def test_tiny_budget_skips_network(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "synthetic-test-key"}), patch("urllib.request.urlopen") as network:
            review = ModelReview(budget=0.000001)
            ctx = context()
            ctx.institution.policy = "Synthetic test policy"
            baseline, fs = evaluate(ctx)
            self.assertEqual(review.review(ctx, baseline, fs)["status"], "budget_skipped")
            network.assert_not_called()

    def test_malformed_model_findings_rejected(self):
        with self.assertRaises(ValueError):
            validate_findings({"findings": ["not an object"]}, context())

    def test_successful_transport_is_validated_and_costed(self):
        ctx = context(documents=[statement()])
        ctx.institution.policy = 'Synthetic policy'
        baseline, fs = evaluate(ctx)
        reply = {'usage': {'prompt_tokens': 100, 'completion_tokens': 100, 'cost': 0.000375},
                 'choices': [{'message': {'content': json.dumps(self.candidate(ctx))}}]}
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'synthetic-test-key'}), patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(reply).encode())) as network:
            model = ModelReview()
            review = model.review(ctx, baseline, fs)
            self.assertEqual(review['status'], 'reviewed')
            self.assertEqual(model.cost, Decimal('0.000375'))
            request = network.call_args.args[0]
            self.assertEqual(request.full_url, 'https://openrouter.ai/api/v1/chat/completions')
            self.assertEqual(json.loads(request.data)['temperature'], 0)

    def test_unusable_response_keeps_baseline(self):
        ctx = context()
        ctx.institution.policy = 'Synthetic policy'
        baseline, fs = evaluate(ctx)
        reply = {'choices': [{'message': {'content': 'not valid json'}}]}
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'synthetic-test-key'}), patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(reply).encode())):
            review = ModelReview().review(ctx, baseline, fs)
            self.assertEqual(review['status'], 'failed')
            self.assertEqual(apply_review(ctx, baseline, fs, review)[0], baseline)


if __name__ == "__main__":
    unittest.main()
