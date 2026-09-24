import os
import unittest
from unittest.mock import patch

from cointrade.ai import analyze_evidence, reserve
from cointrade.config import Config
from cointrade.routing import ASTRA, NO_LLM, select
from cointrade.store import Store


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:', Config(), 'replay')
        self.addCleanup(self.store.db.close)

    @patch('cointrade.ai.request_json')
    @patch.dict(os.environ, {}, clear=True)
    def test_deterministic_work_never_calls_a_model(self, request):
        for task in NO_LLM:
            self.assertIsNone(analyze_evidence(self.store, {}, task)['model'])
        request.assert_not_called()

    def test_explicit_astra_and_auto_tiers_are_distinct(self):
        for task in ASTRA:
            route = select(task)
            self.assertEqual(route.model, 'openai/gpt-6-astra')
            self.assertIsNone(route.tier)
        self.assertEqual(select('wallet_summary').tier, 'low')
        self.assertEqual(select('pattern_analysis').tier, 'medium')
        self.assertEqual(select('pattern_analysis', 'high').tier, 'high')
        with self.assertRaises(ValueError):
            select('debugging', 'low')

    @patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-only'}, clear=True)
    @patch('cointrade.ai.request_json')
    def test_astra_not_routed_or_replaced(self, request):
        request.return_value = {'model': 'openai/gpt-6-astra', 'choices': [{'message': {'content': 'Proposed fix'}}]}
        analyze_evidence(self.store, {'failure': 'example'}, 'debugging')
        payload = request.call_args.args[1]
        self.assertEqual(payload['model'], 'openai/gpt-6-astra')
        self.assertNotIn('plugins', payload)
        self.assertFalse(payload['provider']['allow_fallbacks'])
        self.assertEqual(self.store.db.execute('SELECT requested_model FROM ai_routes').fetchone()[0], 'openai/gpt-6-astra')
        with self.assertRaises(ValueError):
            reserve(self.store.db, self.store.db.execute('SELECT ts FROM ai_calls').fetchone()[0])

    @patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-only'}, clear=True)
    @patch('cointrade.ai.request_json')
    def test_auto_default_has_no_cheap_only_allowlist(self, request):
        request.return_value = {'model': 'provider/model', 'choices': [{'message': {'content': 'Observation'}}]}
        result = analyze_evidence(self.store, {}, 'unusual_activity')
        plugin = request.call_args.args[1]['plugins'][0]
        self.assertEqual(plugin, {'id': 'auto-router', 'cost_tier': 'low'})
        self.assertEqual(result['model'], 'provider/model')

    @patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-only', 'OPENROUTER_ALLOWED_MODELS': '["openai/*"]'}, clear=True)
    @patch('cointrade.ai.request_json')
    def test_optional_user_allowlist(self, request):
        request.return_value = {'model': 'openai/example', 'choices': [{'message': {'content': 'Observation'}}]}
        analyze_evidence(self.store, {}, 'conflicting_signals')
        self.assertEqual(request.call_args.args[1]['plugins'][0]['allowed_models'], ['openai/*'])


if __name__ == '__main__':
    unittest.main()
