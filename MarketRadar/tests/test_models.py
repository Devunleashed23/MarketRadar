"""Factory and LangChain bridge checks; inference uses an in-memory double."""
import importlib
import json
import os
import unittest
from unittest.mock import patch

from langchain_core.messages import HumanMessage

from MarketRadar.cloud_model import CloudModel
from MarketRadar.models import ResearchChatModel


class ModelTests(unittest.TestCase):
    def test_factory_uses_cloud_inference_without_google_credentials(self):
        with patch.dict(os.environ, {'GOOGLE_CLOUD_PROJECT': '', 'MARKETRADAR_PROVIDER': ''}):
            legacy = importlib.import_module('models')
            provider = legacy.get_research_model(model='test-model')
            self.assertIsInstance(provider, CloudModel)
            self.assertEqual(provider.model, 'test-model')
            self.assertIn('Listing Extraction Agent', legacy.system_prompt)
            with self.assertRaisesRegex(RuntimeError, 'GOOGLE_CLOUD_PROJECT'):
                legacy.get_google_model()

    def test_factory_rejects_other_providers(self):
        from models import get_research_model
        with self.assertRaisesRegex(ValueError, 'alternate_clouds'):
            get_research_model(provider='unsupported')
        self.assertIsInstance(get_research_model(provider='alternate'), CloudModel)

    def test_bridge_requests_json_and_returns_framework_tool_call(self):
        class Provider:
            model = 'test-model'

            def complete_json(self, system, prompt, **kwargs):
                request = json.loads(prompt)
                self.assertions = (system, request, kwargs)
                return {'content': '', 'tool_calls': [
                    {'name': 'search_products', 'args': '{"query":"water bottle"}', 'id': 'search-1'}]}

        provider = Provider()
        model = ResearchChatModel(provider=provider).bind_tools([{
            'type': 'function', 'function': {'name': 'search_products', 'description': 'Read product evidence.',
                'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}}}],
            tool_choice='required')
        response = model.invoke([HumanMessage(content='Research water bottles.')])
        self.assertEqual(response.tool_calls[0]['args'], {'query': 'water bottle'})
        self.assertEqual(response.tool_calls[0]['name'], 'search_products')
        system, request, options = provider.assertions
        self.assertIn('Never execute tools yourself', system)
        self.assertEqual(request['tool_choice'], 'required')
        self.assertEqual(options['schema']['properties']['tool_calls']['minItems'], 1)


if __name__ == '__main__':
    unittest.main()
