"""Provider selection, credential isolation and local configuration contracts."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from configuration import load_environment, selected_provider
from model import ModelReview
from engine import evaluate
from serve import Store
from test_system import context


class ConfigurationTests(unittest.TestCase):
    def test_environment_file_honors_process_precedence_and_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('XAI_API_KEY=file-key\nOPENROUTER_API_KEY="quoted-key"\nUNKNOWN_VARIABLE=ignore\nTM_PROVIDER=xai\n', encoding='utf-8')
            with patch.dict(os.environ, {'XAI_API_KEY': 'process-key'}, clear=True):
                load_environment(path)
                self.assertEqual(os.environ['XAI_API_KEY'], 'process-key')
                self.assertEqual(os.environ['OPENROUTER_API_KEY'], 'quoted-key')
                self.assertNotIn('UNKNOWN_VARIABLE', os.environ)
                self.assertEqual(selected_provider(), 'xai')

    def test_provider_selection_is_explicit_then_environment_then_available_key(self):
        cases = [({}, 'openrouter'), ({'XAI_API_KEY': 'key'}, 'xai'),
                 ({'XAI_API_KEY': 'key', 'OPENROUTER_API_KEY': 'other'}, 'openrouter'),
                 ({'TM_PROVIDER': 'xai', 'OPENROUTER_API_KEY': 'other'}, 'xai')]
        for env, expected in cases:
            with self.subTest(env=list(env)), patch.dict(os.environ, env, clear=True):
                self.assertEqual(selected_provider(), expected)
                self.assertEqual(selected_provider('openrouter'), 'openrouter')
        with patch.dict(os.environ, {'TM_PROVIDER': 'unsupported'}, clear=True):
            with self.assertRaises(ValueError): selected_provider()

    def test_xai_wire_format_and_credentials_are_isolated(self):
        ctx = context()
        ctx.institution.policy = 'Synthetic policy'
        baseline, findings = evaluate(ctx)
        reply = {'usage': {'prompt_tokens': 10, 'completion_tokens': 10}, 'choices': [{'message': {'content': json.dumps({'findings': [{
            'alert_id': 'ALERT-0', 'verdict': 'RFI', 'reason_code': 'RFI_SOURCE_OF_FUNDS',
            'detail': 'The source has no independent supporting record.',
            'request': 'one dated source statement reconciling the incoming transfer',
            'evidence': ['TX-A'], 'quotes': []}]})}}]}
        with patch.dict(os.environ, {'XAI_API_KEY': 'xai-test-key'}, clear=True), patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(reply).encode())) as network:
            model = ModelReview(provider='xai')
            result = model.review(ctx, baseline, findings)
            self.assertEqual(result['status'], 'reviewed', result)
            request = network.call_args.args[0]
            self.assertEqual(request.full_url, 'https://api.x.ai/v1/chat/completions')
            body = json.loads(request.data)
            self.assertEqual(body['model'], 'grok-4.3')
            self.assertNotIn('provider', body)
            self.assertNotIn('seed', body)
            self.assertEqual(body['reasoning_effort'], 'none')
            self.assertEqual(ModelReview(provider='openrouter').review(ctx, baseline, findings)['status'], 'unavailable')
            self.assertEqual(network.call_count, 1)

    def test_dashboard_reports_selected_provider_without_exposing_credentials(self):
        with patch.dict(os.environ, {'XAI_API_KEY': 'xai-test-key'}, clear=True), patch.object(Store, 'outputs', return_value={}):
            summary = Store(Path.cwd()).summary()
        self.assertTrue(summary['model_available'])
        self.assertEqual(summary['provider'], 'xai')
        self.assertEqual(summary['model'], 'grok-4.3')
        self.assertNotIn('xai-test-key', json.dumps(summary))


if __name__ == '__main__':
    unittest.main()
