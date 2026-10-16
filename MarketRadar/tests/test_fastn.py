import unittest

from MarketRadar.fastn import FastnClient, expand_template, verify_snapshot


class FastnTests(unittest.TestCase):
    def test_templates_preserve_json_types_and_reject_unknown_fields(self):
        self.assertEqual(expand_template({'input': '{brief}'}, {'brief': {'keyword': 'mug'}}),
                         {'input': {'keyword': 'mug'}})
        with self.assertRaises(ValueError):
            expand_template({'input': '{missing}'}, {})

    def test_discovery_and_tool_error_are_not_success(self):
        calls = []
        def transport(method, **params):
            calls.append((method, params))
            if method == 'list_tools':
                return {'tools': [{'name': 'load', 'inputSchema': {'type': 'object'}}]}
            return {'isError': True, 'content': [{'type': 'text', 'text': 'not connected'}]}
        client = FastnClient({'gateway_url': 'https://mcp.fastn.dev', 'read_tool': 'load'}, transport)
        self.assertEqual(client.discover()['tools'][0]['name'], 'load')
        with self.assertRaises(RuntimeError):
            client.load_brief({'keyword': 'mug'})
        self.assertEqual(calls[-1][1]['name'], 'load')

    def test_readback_requires_exact_persisted_content(self):
        payload = {'run_id': 'abc', 'report': {'summary': 'mug'}, 'evidence': [], 'approved_actions': []}
        self.assertFalse(verify_snapshot({'run_id': 'abc'}, payload))
        self.assertFalse(verify_snapshot(dict(payload, report={'summary': 'wrong'}), payload))
        self.assertTrue(verify_snapshot({'data': payload}, payload))

    def test_no_network_to_unapproved_gateway(self):
        with self.assertRaises(ValueError):
            FastnClient({'gateway_url': 'https://attacker.example/mcp'}).discover()

