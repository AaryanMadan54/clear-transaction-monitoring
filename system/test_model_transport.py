"""Failure isolation, billing, retries, citations and hybrid policy boundaries."""
import copy
import io
import json
import unittest
import urllib.error
from decimal import Decimal
from unittest.mock import patch

from domain import Finding
from engine import compose, evaluate
from model import ModelReview, apply_review, source_passages, structured_format, validate_findings, resolve_passages, retry_delay
from test_system import context, doc, statement, tx


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.ctx = context(documents=[statement()])
        self.ctx.institution.policy = "Synthetic policy"
        self.baseline, self.findings = evaluate(self.ctx)
        self.environment = patch.dict('os.environ', {'OPENROUTER_API_KEY': 'synthetic-test-key'}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def reply(self, cost='0.001'):
        key = next(iter(source_passages(self.ctx)))
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'findings': [{
            'alert_id': 'ALERT-0', 'verdict': 'CLOSE', 'reason_code': 'OWN_ACCOUNT',
            'detail': 'The cited statement proves ownership of the matching counterparty account.',
            'evidence': ['DOC-STATEMENT', 'TX-A'], 'quotes': [{'quote_id': key}]}]})}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 100, 'cost': cost}}

    def response(self, payload):
        return io.BytesIO(json.dumps(payload).encode())

    def error(self, status, message='Synthetic failure', metadata=None, retry=None):
        body = {'error': {'code': status, 'message': message, 'metadata': metadata or {}}}
        return urllib.error.HTTPError('https://openrouter.ai/api/v1/chat/completions', status, message,
                                      {'Retry-After': str(retry)} if retry is not None else {}, self.response(body))

    def review(self, reviewer=None):
        return (reviewer or ModelReview()).review(self.ctx, self.baseline, self.findings)

    def test_passage_id_resolves_to_unchanged_source_text(self):
        # Fixtures use a different statement ID from transport's illustrative response.
        payload = self.reply()
        parsed = json.loads(payload['choices'][0]['message']['content'])
        parsed['findings'][0]['evidence'][0] = self.ctx.docs[0].id
        payload['choices'][0]['message']['content'] = json.dumps(parsed)
        with patch('urllib.request.urlopen', return_value=self.response(payload)) as network:
            result = self.review()
        self.assertEqual(result['status'], 'reviewed', result)
        quote = result['quotes'][0]
        self.assertIn(quote['text'], self.ctx.docs[0].text)
        request = json.loads(network.call_args.args[0].data)
        self.assertEqual(request['response_format']['type'], 'json_schema')
        self.assertEqual(request['response_format']['json_schema']['schema']['properties']['findings']['minItems'], 1)
        self.assertIn('source_passages', json.loads(request['messages'][1]['content']))

    def test_structured_rfi_is_rendered_without_brittle_model_wording(self):
        payload = {'findings': [{'alert_id': 'ALERT-0', 'verdict': 'RFI',
                   'reason_code': 'RFI_SOURCE_OF_FUNDS', 'detail': 'The source remains undocumented.',
                   'request': 'one dated source statement reconciling the incoming transfer',
                   'evidence': ['TX-A'], 'quotes': []}]}
        clean = validate_findings(resolve_passages(payload, {}), self.ctx)
        self.assertTrue(clean[0].detail.startswith('Ask for one dated source statement'))

    def test_selected_quote_adds_its_verified_source_citation(self):
        payload = {'findings': [{'quotes': [{'quote_id': next(iter(source_passages(self.ctx)))}],
                               'evidence': ['TX-A']}]}
        result = resolve_passages(payload, source_passages(self.ctx))
        self.assertIn(self.ctx.docs[0].id, result['findings'][0]['evidence'])

    def test_unknown_passage_reference_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unknown source passage'):
            resolve_passages({'findings': [{'quotes': [{'quote_id': 'invented'}]}]}, source_passages(self.ctx))

    def test_retry_after_http_date_and_invalid_delays(self):
        from datetime import datetime, timezone, timedelta
        from email.utils import format_datetime
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120))
        self.assertGreater(retry_delay(future), 115)
        self.assertEqual(retry_delay('0'), 0)
        for value in ['NaN', 'Infinity', '-1', 'bad header']:
            with self.subTest(value=value): self.assertIsNone(retry_delay(value))

    def test_malformed_envelopes_never_crash_or_change_baseline(self):
        cases = [None, [], {}, {'choices': None}, {'choices': [None]}, {'choices': [1]},
                 {'choices': [{'message': None}]}, {'choices': [{'message': {'content': []}}]},
                 {'usage': [], 'choices': []}, {'usage': {'prompt_tokens': -1}},
                 {'usage': {'cost': 'NaN'}}, {'usage': {'cost': '-1'}},
                 {'usage': {'prompt_tokens': True}}]
        for payload in cases:
            with self.subTest(payload=payload), patch('urllib.request.urlopen', return_value=self.response(payload)):
                result = self.review()
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(apply_review(self.ctx, self.baseline, self.findings, result)[0], self.baseline)

    def test_null_usage_is_tolerated_and_conservatively_reserved(self):
        payload = self.reply()
        payload['usage'] = None
        # A safe RFI does not need a documentary quote.
        row = json.loads(payload['choices'][0]['message']['content'])['findings'][0]
        row.update(verdict='RFI', reason_code='RFI_SOURCE_OF_FUNDS',
                   detail='Ask for the dated source-of-funds statement reconciling the transfer.', evidence=['TX-A'], quotes=[])
        payload['choices'][0]['message']['content'] = json.dumps({'findings': [row]})
        model = ModelReview()
        with patch('urllib.request.urlopen', return_value=self.response(payload)):
            result = self.review(model)
        self.assertEqual(result['status'], 'reviewed', result)
        self.assertTrue(result['cost_estimated'])
        self.assertEqual(model.cost, model.reserved)
        self.assertGreater(model.cost, 0)

    def test_billing_error_opens_circuit_across_200_case_blocks(self):
        model = ModelReview()
        with patch('urllib.request.urlopen', side_effect=self.error(402)) as network:
            first = self.review(model)
            second = self.review(model)
            third = self.review(model.next_block())
        self.assertEqual(first['error_code'], 'billing')
        self.assertEqual(second['status'], 'service_skipped')
        self.assertEqual(third['status'], 'service_skipped')
        self.assertEqual(network.call_count, 1)
        self.assertEqual(model.reserved, 0)

    def test_transient_rejection_retries_once_and_accounts_for_both_attempts(self):
        model = ModelReview()
        payload = self.reply()
        payload['choices'][0]['message']['content'] = json.dumps({'findings': [{
            'alert_id': 'ALERT-0', 'verdict': 'RFI', 'reason_code': 'RFI_SOURCE_OF_FUNDS',
            'detail': 'Ask for a dated statement establishing the source of this transfer.',
            'evidence': ['TX-A'], 'quotes': []}]})
        with patch('urllib.request.urlopen', side_effect=[self.error(503, retry=0), self.response(payload)]) as network:
            result = self.review(model)
        self.assertEqual(result['status'], 'reviewed', result)
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(network.call_count, 2)
        self.assertGreater(model.reserved, model.cost)  # Unknown first-attempt spend remains reserved.
        self.assertLessEqual(model.reserved, model.budget)

    def test_retry_after_does_not_retry_early(self):
        model = ModelReview()
        with patch('urllib.request.urlopen', side_effect=self.error(429, retry=30)) as network:
            result = self.review(model)
            next_result = self.review(model)
        self.assertEqual(result['error_code'], 'rate_limited')
        self.assertEqual(next_result['status'], 'service_skipped')
        self.assertEqual(network.call_count, 1)

    def test_case_moderation_does_not_disable_unrelated_cases(self):
        model = ModelReview()
        with patch('urllib.request.urlopen', side_effect=self.error(403, 'Input flagged by content moderation')):
            result = self.review(model)
        self.assertEqual(result['error_code'], 'content_policy')
        self.assertIsNone(model.service['disabled'])

    def test_inflight_402_is_transient_not_fatal_billing(self):
        model = ModelReview()
        with patch('urllib.request.urlopen', side_effect=[self.error(402, metadata={'limit_source': 'openrouter_in_flight_budget'}, retry=0),
                                                         self.error(429, retry=0)]) as network:
            result = self.review(model)
        self.assertEqual(network.call_count, 2)
        self.assertEqual(result['error_code'], 'rate_limited')
        self.assertIsNone(model.service['disabled'])

    def test_response_errors_refusals_and_truncation_are_distinct(self):
        cases = [({'error': {'code': 402, 'message': 'No balance'}}, 'billing'),
                 ({'choices': [{'error': {'message': 'Generation failed'}}]}, 'generation_error'),
                 ({'choices': [{'finish_reason': 'length'}]}, 'truncated'),
                 ({'choices': [{'message': {'refusal': 'Declined'}}]}, 'refusal')]
        for payload, code in cases:
            with self.subTest(code=code), patch('urllib.request.urlopen', return_value=self.response(payload)):
                self.assertEqual(self.review()['error_code'], code)

    def test_timeout_is_not_retried_and_keeps_spend_reservation(self):
        model = ModelReview()
        with patch('urllib.request.urlopen', side_effect=TimeoutError()) as network:
            result = self.review(model)
        self.assertEqual(network.call_count, 1)
        self.assertEqual(result['error_code'], 'transport')
        self.assertGreater(model.reserved, 0)

    def test_error_messages_redact_key(self):
        with patch('urllib.request.urlopen', side_effect=self.error(401, 'Bad token synthetic-test-key')):
            result = self.review()
        self.assertNotIn('synthetic-test-key', json.dumps(result))

    def test_empty_document_schema_contains_no_empty_enum(self):
        schema = structured_format(context())
        def walk(value):
            if isinstance(value, dict):
                if 'enum' in value:
                    self.assertTrue(value['enum'])
                for child in value.values(): walk(child)
            elif isinstance(value, list):
                for child in value: walk(child)
        walk(schema)
        self.assertEqual(schema['json_schema']['schema']['properties']['findings']['items']['properties']['quotes']['maxItems'], 0)

    def test_bad_environment_rates_and_custom_models_fail_clearly(self):
        for value in ['NaN', '-1', 'not a number', 'Infinity']:
            with self.subTest(value=value), patch.dict('os.environ', {'TM_INPUT_USD_PER_MILLION': value}):
                with self.assertRaises(ValueError): ModelReview()
        with self.assertRaisesRegex(ValueError, 'price ceilings'):
            ModelReview(model='example/custom-model')

    def test_unhashable_model_fields_are_validation_errors(self):
        for field in ['alert_id', 'verdict', 'reason_code']:
            payload = {'findings': [{'alert_id': 'ALERT-0', 'verdict': 'RFI', 'reason_code': 'RFI_SOURCE_OF_FUNDS',
                                    'detail': 'Ask for the dated source statement for the transaction.', 'evidence': ['TX-A'], 'quotes': []}]}
            payload['findings'][0][field] = []
            with self.subTest(field=field), self.assertRaises(ValueError): validate_findings(payload, self.ctx)


class HybridGuardTests(unittest.TestCase):
    def test_reason_code_switch_cannot_close_payment_with_kyc_only(self):
        kyc = doc('DOC-KYC', 'due_diligence_summary', 'Example Person has passed onboarding identity checks.')
        ctx = context(rules=['TM06'], documents=[kyc], transactions=[tx(direction='out', party='Example Supplier')])
        for code in ['OWN_ACCOUNT', 'WITHIN_PROFILE', 'DOCUMENTED_EVENT', 'THIRD_PARTY_DOCUMENTED', 'RETURNING_CUSTOMER']:
            baseline = [Finding('ALERT-0', 'RFI', 'RFI_PURPOSE_DOCUMENTS', 'Ask for the invoice', ['TX-A'], supported=False)]
            result = compose(ctx, baseline)
            candidate = Finding('ALERT-0', 'CLOSE', code, 'The KYC allegedly explains the payment in full.', ['DOC-KYC', 'TX-A'])
            review = {'status': 'reviewed', 'findings': [candidate.as_dict()]}
            with self.subTest(code=code):
                self.assertNotEqual(apply_review(ctx, result, baseline, review)[0]['verdict'], 'CLOSE')
                self.assertTrue(review.get('rejected_closures'))

    def test_reconciled_invoice_can_resolve_an_unsupported_check(self):
        invoice = doc('DOC-INVOICE', 'supplier_invoice', '# Invoice from Example Supplier\n**Dated:** 2026-09-15\n**Total:** £12,000.00\n')
        ctx = context(rules=['TM06'], documents=[invoice], transactions=[tx(direction='out', party='Example Supplier')])
        baseline = [Finding('ALERT-0', 'RFI', 'RFI_PURPOSE_DOCUMENTS', 'Ask for the payment purpose', ['TX-A'], supported=False)]
        result = compose(ctx, baseline)
        candidate = Finding('ALERT-0', 'CLOSE', 'INVOICE_MATCHES', 'The dated invoice reconciles the supplier and the payment amount.', ['DOC-INVOICE', 'TX-A'])
        reviewed, _ = apply_review(ctx, result, baseline, {'status': 'reviewed', 'findings': [candidate.as_dict()]})
        self.assertEqual(reviewed['verdict'], 'CLOSE')

    def test_old_invoice_cannot_be_rescued_by_model_reason_code(self):
        invoice = doc('DOC-INVOICE', 'supplier_invoice', '# Invoice from Example Supplier\n**Dated:** 2024-09-15\n**Total:** £12,000.00\n')
        invoice.meta['dated'] = '2024-09-15'
        ctx = context(rules=['TM06'], documents=[invoice], transactions=[tx(direction='out', party='Example Supplier')])
        baseline = [Finding('ALERT-0', 'RFI', 'RFI_PURPOSE_DOCUMENTS', 'Ask for the current invoice', ['TX-A'], supported=False)]
        result = compose(ctx, baseline)
        candidate = Finding('ALERT-0', 'CLOSE', 'INVOICE_MATCHES', 'The old invoice allegedly explains the new payment.', ['DOC-INVOICE', 'TX-A'])
        reviewed, _ = apply_review(ctx, result, baseline, {'status': 'reviewed', 'findings': [candidate.as_dict()]})
        self.assertNotEqual(reviewed['verdict'], 'CLOSE')

    def test_subscription_cannot_bypass_financial_proof(self):
        gift = doc('DOC-GIFT', 'gift_letter', 'A gift of £100.00 was sent by a different person on 2026-09-15.')
        payment = tx(party='Example Donor')
        payment['channel'] = 'fund_subscription'
        ctx = context(rules=['TM08'], documents=[gift], transactions=[payment], name='investment')
        baseline = [Finding('ALERT-0', 'RFI', 'RFI_COUNTERPARTY_DETAILS', 'Ask for the funding agreement', ['TX-A'], supported=False)]
        result = compose(ctx, baseline)
        candidate = Finding('ALERT-0', 'CLOSE', 'THIRD_PARTY_DOCUMENTED', 'The unrelated gift allegedly explains this subscription.', ['DOC-GIFT', 'TX-A'])
        reviewed, _ = apply_review(ctx, result, baseline, {'status': 'reviewed', 'findings': [candidate.as_dict()]})
        self.assertNotEqual(reviewed['verdict'], 'CLOSE')

    def test_invalid_composed_review_cannot_abort_case_processing(self):
        ctx = context()
        baseline, findings = evaluate(ctx)
        review = {'status': 'reviewed', 'findings': [{'bad': 'shape'}]}
        result, _ = apply_review(ctx, baseline, findings, review)
        self.assertEqual(result, baseline)
        self.assertEqual(review['error_code'], 'policy_validation')


if __name__ == '__main__':
    unittest.main()
