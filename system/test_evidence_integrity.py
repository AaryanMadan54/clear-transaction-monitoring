"""Regression tests for evidence capacity, payment linkage and case-level RFIs."""
import unittest

from domain import Finding
from engine import cash_records, compose, evaluate, held_payment_check
from matching import allocate
from model import apply_review
from test_system import context, doc, tx


def invoice(dated='2026-09-14', reference='INV-FRESH'):
    record = doc('DOC-INVOICE', 'supplier_invoice',
                 f'# Invoice {reference} from Example Supplier\n\n**Dated:** {dated}\n'
                 '**Supplier:** Example Supplier\n**Amount:** £12,000.00\n')
    record.meta['dated'] = dated
    return record


class EvidenceIntegrityTests(unittest.TestCase):
    def held_context(self, record):
        payment = tx(amount=12000, direction='out', party='Example Supplier')
        return context(rules=['TM07'], documents=[record], transactions=[payment],
                       name='payments_fx', held='TX-A')

    def test_two_year_old_invoice_cannot_clear_current_held_payment(self):
        findings = held_payment_check(self.held_context(invoice('2024-09-14')))
        self.assertEqual(findings[0].verdict, 'RFI')

    def test_contemporaneous_invoice_can_clear_current_held_payment(self):
        self.assertEqual(held_payment_check(self.held_context(invoice()))[0].verdict, 'CLOSE')

    def test_conflicting_explicit_invoice_reference_is_not_ignored(self):
        ctx = self.held_context(invoice(reference='INV-OTHER'))
        ctx.transactions[0]['reference'] = 'INV-DIFFERENT'
        self.assertEqual(held_payment_check(ctx)[0].verdict, 'RFI')

    def test_one_takings_row_cannot_explain_two_identical_deposits(self):
        record = doc('DOC-TAKINGS', 'takings_book',
                     '| Date | Market | Cash topped up |\n|---|---|---|\n'
                     '| 15 September 2026 | Example Market | £900.00 |')
        deposits = [tx(key=key, amount=900, party=None) for key in ['TX-ONE', 'TX-TWO']]
        for payment in deposits:
            payment['channel'] = 'cash_topup'
        ctx = context(rules=['TM03'], documents=[record], transactions=deposits, name='emoney')
        self.assertEqual(cash_records(ctx, deposits), [])
        self.assertNotEqual(evaluate(ctx)[0]['verdict'], 'CLOSE')

    def test_distinct_takings_rows_can_explain_identical_deposits(self):
        record = doc('DOC-TAKINGS', 'takings_book',
                     '| Date | Market | Cash topped up |\n|---|---|---|\n'
                     '| 15 September 2026 | Example North Market | £900.00 |\n'
                     '| 15 September 2026 | Example South Market | £900.00 |')
        deposits = [tx(key=key, amount=900, party=None) for key in ['TX-ONE', 'TX-TWO']]
        for payment in deposits:
            payment['channel'] = 'cash_topup'
        ctx = context(rules=['TM03'], documents=[record], transactions=deposits, name='emoney')
        self.assertEqual(evaluate(ctx)[0]['verdict'], 'CLOSE')

    def test_funding_relationship_and_beneficiary_records_are_not_one_rfi(self):
        incoming = tx(key='TX-IN', party='Example Funding Company')
        outgoing = tx(key='TX-OUT', direction='out', party='Example Contractor')
        ctx = context(rules=['TM08', 'TM04'], transactions=[incoming, outgoing], name='payments_fx')
        ctx.alerts[0]['transaction_ids'] = ['TX-IN']
        ctx.alerts[1]['transaction_ids'] = ['TX-OUT']
        findings = [
            Finding('ALERT-0', 'RFI', 'RFI_COUNTERPARTY_DETAILS', 'Ask for proof of the funding relationship', ['TX-IN'], supported=False),
            Finding('ALERT-1', 'RFI', 'RFI_PURPOSE_DOCUMENTS', 'Ask for the contractor payment file', ['TX-OUT'], supported=False),
        ]
        result = compose(ctx, findings)
        self.assertEqual(result['verdict'], 'ESCALATE')
        self.assertNotIn('Ask for', result['reason'])

    def test_one_source_document_resolves_two_alerts_on_the_same_cash(self):
        payment = tx(amount=12000, party=None)
        payment['channel'] = 'cash_deposit'
        ctx = context(rules=['TM01', 'TM05'], transactions=[payment])
        findings = [Finding(a['alert_id'], 'RFI', 'RFI_SOURCE_OF_FUNDS',
                            'Ask for the dated cash sale receipt', ['TX-A'], supported=False) for a in ctx.alerts]
        result = compose(ctx, findings)
        self.assertEqual(result['verdict'], 'RFI')
        self.assertEqual(result['reason'].count('Ask for'), 1)

    def test_old_invoice_with_matching_payment_reference_is_linked(self):
        ctx = self.held_context(invoice('2024-09-14', reference='INV-OLD'))
        ctx.transactions[0]['reference'] = 'INV-OLD'
        self.assertEqual(held_payment_check(ctx)[0].verdict, 'CLOSE')

    def test_old_invoice_with_current_explicit_due_date_is_linked(self):
        record = invoice('2024-09-14')
        record.text += '\n**Due date:** 15 September 2026\n'
        record.__post_init__()
        self.assertEqual(held_payment_check(self.held_context(record))[0].verdict, 'CLOSE')

    def test_an_explicit_wrong_due_date_overrides_a_matching_reference(self):
        record = invoice('2024-09-14', reference='INV-OLD')
        record.text += '\n**Due date:** 15 September 2024\n'
        record.__post_init__()
        ctx = self.held_context(record)
        ctx.transactions[0]['reference'] = 'INV-OLD'
        self.assertEqual(held_payment_check(ctx)[0].verdict, 'RFI')

    def test_old_invoice_reference_does_not_clear_a_second_settlement(self):
        ctx = self.held_context(invoice('2024-09-14', reference='INV-OLD'))
        ctx.transactions[0]['reference'] = 'INV-OLD'
        earlier = tx(key='TX-SETTLED', amount=12000, direction='out', party='Example Supplier')
        earlier['reference'] = 'INV-OLD'
        earlier['booked_at'] = '2024-09-15T10:00:00Z'
        ctx.transactions.insert(0, earlier)
        self.assertEqual(held_payment_check(ctx)[0].verdict, 'RFI')

    def test_current_report_does_not_refresh_a_stale_takings_row(self):
        record = doc('DOC-TAKINGS', 'takings_book',
                     '| Date | Cash topped up |\n|---|---|\n| 15 September 2024 | £900.00 |')
        payment = tx(amount=900, party=None)
        payment['channel'] = 'cash_topup'
        ctx = context(rules=['TM03'], documents=[record], transactions=[payment], name='emoney')
        self.assertEqual(cash_records(ctx, [payment]), [])

    def test_explicit_each_amount_and_count_allow_two_deposits(self):
        record = doc('DOC-TAKINGS', 'takings_book',
                     '| Date | Deposits | Amount each |\n|---|---|---|\n| 15 September 2026 | 2 | £900.00 |')
        deposits = [tx(key=key, amount=900, party=None) for key in ['TX-ONE', 'TX-TWO']]
        for payment in deposits:
            payment['channel'] = 'cash_topup'
        ctx = context(rules=['TM03'], documents=[record], transactions=deposits, name='emoney')
        self.assertEqual(evaluate(ctx)[0]['verdict'], 'CLOSE')

    def test_a_single_schedule_row_does_not_cover_two_payments(self):
        record = doc('DOC-SCHEDULE', 'contract_schedule',
                     '# Schedule for Example Supplier\n| Due | Amount |\n|---|---|\n| 15 September 2026 | £9,500.00 |')
        payments = [tx(key=key, amount=9500, direction='out', party='Example Supplier') for key in ['TX-ONE', 'TX-TWO']]
        ctx = context(rules=['TM03'], documents=[record], transactions=payments, name='payments_fx')
        result, findings = evaluate(ctx)
        self.assertNotEqual(result['verdict'], 'CLOSE')
        self.assertTrue(any(f.check == 'case:coverage' for f in findings))

    def test_repeated_alerts_can_rely_on_the_same_actual_transaction(self):
        receipt = doc('DOC-SALE', 'sale_receipt', 'Example Person sold a bicycle for £12,000.00 in cash.')
        payment = tx(party=None)
        payment['channel'] = 'cash_deposit'
        ctx = context(rules=['TM01', 'TM05'], documents=[receipt], transactions=[payment])
        self.assertEqual(evaluate(ctx)[0]['verdict'], 'CLOSE')

    def test_allocation_can_reassign_overlapping_date_matches(self):
        record = doc('DOC-TAKINGS', 'takings_book',
                     '| Date | Cash topped up |\n|---|---|\n| 15 September 2026 | £900.00 |\n| 14 September 2026 | £900.00 |')
        early = tx(key='TX-EARLY', amount=900, party=None)
        late = tx(key='TX-LATE', amount=900, party=None)
        late['booked_at'] = '2026-09-22T10:00:00Z'
        self.assertTrue(allocate([record], [early, late], rows_only=True).complete)
        self.assertTrue(allocate([record], [late, early], rows_only=True).complete)

    def test_source_and_relationship_of_one_gift_have_one_rfi(self):
        incoming = tx(party='Example Donor')
        incoming['reference'] = 'GIFT'
        ctx = context(rules=['TM01', 'TM08'], transactions=[incoming], name='investment')
        result, _ = evaluate(ctx)
        self.assertEqual(result['verdict'], 'RFI')
        self.assertIn('signed gift declaration', result['reason'])
        self.assertEqual(result['reason'].lower().count('ask for'), 1)

    def test_identical_rfi_codes_do_not_hide_independent_payments(self):
        payments = [tx(key='TX-ONE', direction='out', party='Example Supplier'),
                    tx(key='TX-TWO', direction='out', party='Another Supplier')]
        ctx = context(rules=['TM06', 'TM06'], transactions=payments, name='payments_fx')
        for alert, payment in zip(ctx.alerts, payments):
            alert['transaction_ids'] = [payment['transaction_id']]
        findings = [Finding(a['alert_id'], 'RFI', 'RFI_PURPOSE_DOCUMENTS',
                            'Ask for the invoice for this payment', a['transaction_ids'], supported=False) for a in ctx.alerts]
        self.assertEqual(compose(ctx, findings)['verdict'], 'ESCALATE')

    def test_one_order_contract_can_resolve_inbound_and_outbound_gaps(self):
        payments = [tx(key='TX-IN', party='Example Funder'), tx(key='TX-OUT', direction='out', party='Example Supplier')]
        for payment in payments:
            payment['reference'] = 'PO 123456'
        ctx = context(rules=['TM08', 'TM04'], transactions=payments, name='payments_fx')
        for alert, payment in zip(ctx.alerts, payments):
            alert['transaction_ids'] = [payment['transaction_id']]
        result, _ = evaluate(ctx)
        self.assertEqual(result['verdict'], 'RFI')
        self.assertIn('executed contract for PO 123456', result['reason'])

    def test_model_cannot_reuse_a_row_to_force_closure(self):
        record = doc('DOC-SCHEDULE', 'contract_schedule',
                     '# Schedule for Example Supplier\n| Due | Amount |\n|---|---|\n| 15 September 2026 | £9,500.00 |')
        payments = [tx(key=key, amount=9500, direction='out', party='Example Supplier') for key in ['TX-ONE', 'TX-TWO']]
        ctx = context(rules=['TM03'], documents=[record], transactions=payments, name='payments_fx')
        baseline = [Finding('ALERT-0', 'RFI', 'RFI_PURPOSE_DOCUMENTS', 'Ask for the payment schedule', ['TX-ONE', 'TX-TWO'], supported=False)]
        result = compose(ctx, baseline)
        proposal = Finding('ALERT-0', 'CLOSE', 'INVOICE_MATCHES', 'The schedule allegedly explains both payments', ['DOC-SCHEDULE', 'TX-ONE', 'TX-TWO'])
        review = {'status': 'reviewed', 'findings': [proposal.as_dict()]}
        self.assertNotEqual(apply_review(ctx, result, baseline, review)[0]['verdict'], 'CLOSE')

    def test_original_outgoing_payment_does_not_consume_its_refund_letter(self):
        refund = doc('DOC-REFUND', 'venue_email', '# Refund from Example Venue\nWe are returning the £12,000.00 deposit for BOOKING 12345.')
        incoming = tx(party='Example Venue')
        incoming['reference'] = 'REFUND BOOKING 12345'
        earlier = tx(key='TX-ORIGINAL', direction='out', party='Example Venue')
        earlier['reference'] = 'BOOKING 12345'
        earlier['booked_at'] = '2026-03-01T10:00:00Z'
        ctx = context(rules=['TM06'], documents=[refund], transactions=[earlier, incoming])
        ctx.alerts[0]['transaction_ids'] = ['TX-A']
        self.assertEqual(evaluate(ctx)[0]['verdict'], 'CLOSE')

    def test_one_gift_declaration_can_resolve_a_donor_identity_match_too(self):
        incoming = tx(party='Example Donor')
        incoming['reference'] = 'GIFT'
        ctx = context(rules=['TM01', 'TM08', 'TM22'], transactions=[incoming], name='investment')
        ctx.institution.list_entries['LIST-UNKNOWN'] = {'entry_id': 'LIST-UNKNOWN', 'kind': 'individual',
                                                      'name': 'Example Donor', 'date_of_birth': '1960-02-12'}
        ctx.alerts[2]['measured'].update(list_entry='LIST-UNKNOWN', matched_on='name')
        result, _ = evaluate(ctx)
        self.assertEqual(result['verdict'], 'RFI')
        self.assertIn("donor's full name, date of birth and address", result['reason'])

    def test_gift_identity_and_relationship_need_only_one_declaration_without_profile_alert(self):
        incoming = tx(party='Example Donor')
        incoming['reference'] = 'GIFT'
        ctx = context(rules=['TM08', 'TM22'], transactions=[incoming], name='investment')
        ctx.institution.list_entries['LIST-UNKNOWN'] = {'entry_id': 'LIST-UNKNOWN', 'kind': 'individual', 'name': 'Example Donor'}
        ctx.alerts[1]['measured'].update(list_entry='LIST-UNKNOWN', matched_on='name')
        result, _ = evaluate(ctx)
        self.assertEqual(result['verdict'], 'RFI')
        self.assertEqual(result['reason'].lower().count('ask for'), 1)

    def booking_context(self, withdrawal):
        record = doc('DOC-BOOKING', 'booking_confirmation',
                     '# Booking from Example Travel\n**Booking reference:** GT12345\n'
                     '**Price:** £50.00 per person, £100.00 in total\n'
                     '1. Example Person\n2. Example Friend\n')
        incoming = tx(key='TX-CONTRIBUTION', amount=50, party='Example Friend')
        outgoing = tx(key='TX-TRAVEL', amount=100, direction='out', party='Example Travel')
        cash = tx(key='TX-CASH', amount=withdrawal, direction='out', party=None)
        cash['channel'] = 'cash_withdrawal'
        old = tx(key='TX-OLD-CASH', amount=50, direction='out', party=None)
        old['channel'] = 'cash_withdrawal'
        old['booked_at'] = '2026-08-01T10:00:00Z'
        ctx = context(rules=['TM02'], documents=[record], transactions=[old, incoming, outgoing, cash], name='emoney')
        ctx.alerts[0]['transaction_ids'] = ['TX-CONTRIBUTION', 'TX-TRAVEL', 'TX-CASH']
        return ctx

    def test_normal_historical_atm_spend_does_not_need_a_travel_invoice(self):
        self.assertEqual(evaluate(self.booking_context(50))[0]['verdict'], 'CLOSE')

    def test_a_large_unexplained_withdrawal_is_not_hidden_by_a_booking(self):
        self.assertNotEqual(evaluate(self.booking_context(5000))[0]['verdict'], 'CLOSE')

    def test_model_cannot_claim_an_invoice_match_using_only_kyc(self):
        kyc = doc('DOC-KYC', 'due_diligence_summary', 'Example Person has passed onboarding identity checks.')
        ctx = context(rules=['TM06'], documents=[kyc], transactions=[tx(direction='out', party='Example Supplier')])
        baseline = [Finding('ALERT-0', 'RFI', 'RFI_PURPOSE_DOCUMENTS', 'Ask for the invoice', ['TX-A'], supported=False)]
        result = compose(ctx, baseline)
        proposal = Finding('ALERT-0', 'CLOSE', 'INVOICE_MATCHES', 'The KYC allegedly explains the payment', ['DOC-KYC', 'TX-A'])
        self.assertNotEqual(apply_review(ctx, result, baseline, {'status': 'reviewed', 'findings': [proposal.as_dict()]})[0]['verdict'], 'CLOSE')

    def test_ambiguous_item_count_is_not_payment_capacity(self):
        record = doc('DOC-RECORD', 'takings_book',
                     '| Date | Count | Amount each |\n|---|---|---|\n| 15 September 2026 | 2 | £900.00 |')
        payments = [tx(key=key, amount=900, party=None) for key in ['TX-ONE', 'TX-TWO']]
        self.assertFalse(allocate([record], payments, rows_only=True).complete)

    def test_schedule_header_order_reference_applies_to_its_rows(self):
        record = doc('DOC-SCHEDULE', 'contract_schedule',
                     '# Schedule for Example Supplier, PO 55555\n| Due | Amount |\n|---|---|\n| 15 September 2026 | £9,500.00 |')
        payment = tx(amount=9500, direction='out', party='Example Supplier')
        payment['reference'] = 'PO 99999'
        self.assertFalse(allocate([record], [payment]).complete)


if __name__ == '__main__':
    unittest.main()
