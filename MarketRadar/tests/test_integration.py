"""Live orchestration doubles and a real local MCP protocol fixture.

The HTTP fixture is deliberately localhost-only. It proves SDK interoperability,
not connectivity to fastn or any seller account. No model turn is consumed.
"""
from collections import deque
import copy
import importlib.util
from http.client import HTTPConnection
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from MarketRadar.core import Radar, validate_report
from MarketRadar.fastn import FastnClient


FIXTURE_KEY = 'local-integration-fixture-key'
CONNECTION = {'gateway_url': 'http://127.0.0.1:1/mcp', 'api_key': FIXTURE_KEY,
              'read_tool': 'read_context', 'read_arguments': {'brief': '{brief}', 'run_id': '{run_id}'},
              'write_tool': 'store_report', 'write_arguments': {'payload': '{payload}'},
              'verify_tool': 'read_report', 'verify_arguments': {'run_id': '{run_id}'}}
EVIDENCE = [
    {'id': 'planner-blocked', 'source': 'keyword_planner', 'status': 'login_required', 'data': {'keywords': []}},
    {'id': 'trends-live', 'source': 'google_trends', 'status': 'live',
     'data': {'trends': [{'date': '2026-10-01', 'value': 67, 'term': 'travel mug'}]},
     'settings': {'country': 'US', 'date_range': 'today 12-m'}},
]
ANALYSIS = {'summary': 'One Trends observation is available; Planner needs login.',
            'observations': ['The captured normalized interest value is 67.'],
            'limitations': ['No absolute search volume or sales evidence is available.']}
ACTION = {'id': 'review-trends', 'title': 'Review the interest series',
          'reason': 'A single normalized observation is insufficient to establish a growth trend.',
          'evidence_ids': ['trends-live']}


class StubModel:
    def __init__(self, responses, emit=None):
        self.responses, self.calls = deque(responses), []
        self.emit = emit or (lambda *args, **kwargs: None)

    def complete_json(self, system, prompt, **kwargs):
        self.calls.append({'system': system, 'prompt': prompt, **kwargs})
        self.emit('model_response', detail='Explicit integration test double', usage={'input_tokens': 10, 'output_tokens': 3})
        return copy.deepcopy(self.responses.popleft())


class StubBrowser:
    def __init__(self, evidence, emit=None):
        self.evidence, self.briefs = evidence, []
        self.emit = emit or (lambda *args, **kwargs: None)

    def collect(self, brief, model, stop=None):
        self.briefs.append(copy.deepcopy(brief))
        self.emit({'source': 'fixture', 'message': 'Explicit browser test double'})
        return copy.deepcopy(self.evidence)


class StubFastn:
    def __init__(self):
        self.loads, self.writes = [], []
        self.write_error = None

    def load_brief(self, brief, identity):
        self.loads.append((copy.deepcopy(brief), identity))
        return {'brief': {'keyword': 'insulated travel mug'}, 'context': 'Explicit workspace test double'}

    def store_run(self, payload):
        self.writes.append(copy.deepcopy(payload))
        if self.write_error:
            raise self.write_error
        return {'status': 'verified', 'verified': True, 'detail': 'Explicit fixture readback matched.'}


class WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'MARKETRADAR_FASTN_API_KEY': '', 'MARKETRADAR_FASTN_URL': ''})
        environment.start()
        self.addCleanup(environment.stop)
        # These fixtures cover workspace/history writes, not agent reasoning.
        # A real DeepAgents delegation/tool fixture lives in test_original_agents.
        def research_fixture(brief, model, browser, *, stop=None, emit=None, on_evidence=None):
            evidence = browser.collect(brief, model, stop=stop)
            if on_evidence:
                on_evidence(evidence)
            report = {'summary': 'Source access required.', 'observations': [], 'limitations': [], 'recommendations': []}
            if any(record.get('status') == 'live' for record in evidence):
                report = model.complete_json('Fixture analysis', json.dumps(brief), stop=stop)
                actions = model.complete_json('Fixture follow-ups', json.dumps(report), stop=stop)
                report = dict(report, recommendations=actions.get('recommendations', []))
            return evidence, validate_report(report, evidence)
        workflow = patch('MarketRadar.agents.run_research', side_effect=research_fixture)
        workflow.start()
        self.addCleanup(workflow.stop)

    def make_radar(self, folder, evidence=None, responses=None):
        client = StubFastn()
        model = StubModel(responses if responses is not None else [ANALYSIS, {'recommendations': [ACTION]}])
        browser = StubBrowser(EVIDENCE if evidence is None else evidence)
        def model_factory(**kwargs):
            model.emit = kwargs['emit']
            return model
        def browser_factory(**kwargs):
            browser.emit = kwargs['emit']
            return browser
        radar = Radar(folder, model_factory, browser_factory, lambda settings: client)
        radar.settings.save(CONNECTION)
        return radar, client, model, browser

    def completed_run(self, radar):
        run = radar.start({'keyword': 'travel mug', 'mode': 'live', 'use_fastn': True})
        radar.wait(run['id'], timeout=3)
        self.assertFalse(radar.threads[run['id']].is_alive(), 'The fixture job must finish within its bound')
        return radar.get(run['id'])

    def test_live_pipeline_handoffs_partial_evidence_and_approved_write_dedup(self):
        with tempfile.TemporaryDirectory() as folder:
            radar, client, model, browser = self.make_radar(folder)
            result = self.completed_run(radar)
            self.assertEqual(result['state'], 'completed', result['error'])
            self.assertEqual(browser.briefs[0]['keyword'], 'insulated travel mug')
            self.assertEqual(len(client.loads), 1)
            self.assertEqual(len(model.calls), 2)
            self.assertEqual(result['evidence'][0]['data']['keywords'], [])
            self.assertIn('partial evidence', ' '.join(result['report']['limitations']))
            self.assertTrue(all(ref == 'trends-live' for rec in result['report']['recommendations'] for ref in rec['evidence_ids']))
            self.assertEqual(client.writes, [], 'Research completion must not publish automatically')
            self.assertTrue(any(event.get('usage') for event in result['events']))
            with self.assertRaises(ValueError):
                radar.save(result['id'], ['unapproved-or-invented'])
            radar.approve(result['id'], ['review-trends'])
            saved = radar.save(result['id'], ['review-trends'])
            self.assertEqual(saved['status'], 'verified')
            self.assertEqual(client.writes[0]['approved_actions'], [ACTION])
            self.assertEqual(radar.save(result['id'], ['review-trends']), saved)
            self.assertEqual(len(client.writes), 1, 'Repeated save must not duplicate a destination write')

    def test_source_failure_fixture_produces_no_metrics_or_recommendations(self):
        failed = [{'id': 'planner', 'source': 'keyword_planner', 'status': 'login_required', 'data': {}},
                  {'id': 'trends', 'source': 'google_trends', 'status': 'unavailable', 'data': {}}]
        with tempfile.TemporaryDirectory() as folder:
            radar, client, model, _ = self.make_radar(folder, evidence=failed, responses=[])
            result = self.completed_run(radar)
            self.assertEqual(result['state'], 'completed')
            self.assertEqual(result['evidence'], failed)
            self.assertEqual(result['report']['observations'], [])
            self.assertEqual(result['report']['recommendations'], [])
            self.assertEqual(model.calls, [])
            self.assertNotIn('synthetic', json.dumps(result))
            self.assertEqual(client.writes, [])

    def test_recommendation_cannot_cite_a_failed_source(self):
        unsupported = dict(ACTION, evidence_ids=['planner-blocked'])
        with tempfile.TemporaryDirectory() as folder:
            radar, client, _, _ = self.make_radar(folder, responses=[ANALYSIS, {'recommendations': [unsupported]}])
            result = self.completed_run(radar)
            self.assertEqual(result['state'], 'failed')
            self.assertIn('source evidence', result['error'])
            self.assertIsNone(result['report'])
            self.assertEqual(client.writes, [])

    def test_cancelled_collection_does_not_analyze_or_save(self):
        entered = threading.Event()
        class BlockingBrowser(StubBrowser):
            def collect(self, brief, model, stop=None):
                entered.set()
                if not stop.wait(2):
                    raise RuntimeError('Fixture cancellation did not arrive in time')
                raise RuntimeError('Fixture collection cancelled')
        with tempfile.TemporaryDirectory() as folder:
            radar, client, model, _ = self.make_radar(folder)
            radar.browser_factory = lambda **kwargs: BlockingBrowser([], **kwargs)
            run = radar.start({'keyword': 'travel mug', 'mode': 'live', 'use_fastn': True})
            self.assertTrue(entered.wait(1))
            radar.cancel(run['id'])
            radar.wait(run['id'], timeout=3)
            self.assertEqual(radar.get(run['id'])['state'], 'cancelled')
            self.assertEqual(model.calls, [])
            self.assertEqual(client.writes, [])

    def test_uncertain_write_cannot_be_replayed_automatically(self):
        with tempfile.TemporaryDirectory() as folder:
            radar, client, _, _ = self.make_radar(folder)
            result = self.completed_run(radar)
            client.write_error = RuntimeError('Destination disconnected after receiving the write')
            radar.approve(result['id'], [])
            with self.assertRaises(RuntimeError):
                radar.save(result['id'], [])
            self.assertEqual(radar.get(result['id'])['sync']['status'], 'unknown')
            with self.assertRaisesRegex(ValueError, 'previous write may have completed'):
                radar.save(result['id'], [])
            self.assertEqual(len(client.writes), 1)

    def test_config_and_failed_load_do_not_expose_the_configured_key(self):
        with tempfile.TemporaryDirectory() as folder:
            radar, client, _, _ = self.make_radar(folder)
            def fail(*args):
                raise RuntimeError('Tool failed with Bearer ' + FIXTURE_KEY)
            client.load_brief = fail
            result = self.completed_run(radar)
            self.assertEqual(result['state'], 'failed')
            self.assertNotIn(FIXTURE_KEY, json.dumps(radar.settings.public()))
            self.assertNotIn(FIXTURE_KEY, json.dumps(result))
            self.assertIn('[redacted]', result['error'])

    def test_recommendation_ids_remain_unique_after_normalization(self):
        prefix = 'x' * 80
        report = dict(ANALYSIS, recommendations=[dict(ACTION, id=prefix + 'a'), dict(ACTION, id=prefix + 'b')])
        try:
            result = validate_report(report, EVIDENCE)
        except ValueError:
            return
        ids = [rec['id'] for rec in result['recommendations']]
        self.assertEqual(len(ids), len(set(ids)), 'Distinct input IDs must never collapse into ambiguous saved action IDs')

    def test_model_events_support_the_message_keyword(self):
        with tempfile.TemporaryDirectory() as folder:
            radar, _, model, _ = self.make_radar(folder)
            original = model.complete_json
            def infer(*args, **kwargs):
                model.emit('provider_status', message='Provider progress fixture')
                return original(*args, **kwargs)
            model.complete_json = infer
            result = self.completed_run(radar)
            self.assertEqual(result['state'], 'completed', result['error'])
            self.assertTrue(any(e['message'] == 'Provider progress fixture' for e in result['events']))


class FastnRedactionIntegrationTests(unittest.TestCase):
    def test_tool_error_and_readback_error_redact_the_key(self):
        def transport(operation, **params):
            if params['name'] == 'store_report':
                return {'content': [{'type': 'text', 'text': '{"saved":true}'}]}
            return {'isError': True, 'content': [{'type': 'text', 'text': 'Rejected credential ' + FIXTURE_KEY}]}
        client = FastnClient(CONNECTION, transport=transport)
        try:
            client.load_brief({'keyword': 'mug'})
        except RuntimeError as exc:
            self.assertNotIn(FIXTURE_KEY, str(exc))
        else:
            self.fail('Tool error must not be reported as successful context loading')
        payload = {'run_id': 'fixture-run', 'brief': {'keyword': 'mug'}, 'report': ANALYSIS,
                   'evidence': EVIDENCE, 'approved_actions': []}
        self.assertNotIn(FIXTURE_KEY, json.dumps(client.store_run(payload)))

    def test_successful_tool_payload_does_not_expose_the_key(self):
        client = FastnClient(CONNECTION, transport=lambda *args, **kwargs:
            {'structuredContent': {'context': {'diagnostic': 'Echoed Bearer ' + FIXTURE_KEY}}})
        self.assertNotIn(FIXTURE_KEY, json.dumps(client.load_brief({'keyword': 'mug'})))


@unittest.skipUnless(importlib.util.find_spec('mcp') and importlib.util.find_spec('uvicorn'), 'Optional MCP/uvicorn fixture dependencies absent')
class LocalMcpProtocolTests(unittest.TestCase):
    """An actual MCP SDK server/client exchange, never an external fastn claim."""
    @classmethod
    def setUpClass(cls):
        from mcp.server.fastmcp import FastMCP
        import uvicorn
        cls.records, cls.authorization_headers = {}, []
        cls.corrupt_readback = False
        mcp = FastMCP('Explicit local protocol fixture', host='127.0.0.1', stateless_http=True,
                      json_response=True, log_level='ERROR')

        @mcp.tool()
        def read_context(brief: dict, run_id: str) -> dict:
            """Read a fixture brief, explicitly unrelated to a real account."""
            return {'brief': brief, 'run_id': run_id, 'fixture': True}

        @mcp.tool()
        def store_report(payload: dict) -> dict:
            """Persist only in this test's in-memory fixture."""
            cls.records[payload['run_id']] = copy.deepcopy(payload)
            return {'stored': payload['run_id'], 'fixture': True}

        @mcp.tool()
        def read_report(run_id: str) -> dict:
            """Read the exact fixture snapshot back."""
            value = copy.deepcopy(cls.records[run_id])
            if cls.corrupt_readback:
                value['report']['summary'] = 'Different report despite matching run ID'
            return {'snapshot': json.dumps(value)}

        @mcp.tool()
        def rejected_tool() -> dict:
            """Exercise an MCP isError response."""
            raise ValueError('Explicit fixture refusal')

        app = mcp.streamable_http_app()
        async def record_headers(scope, receive, send):
            if scope['type'] == 'http':
                cls.authorization_headers.append(dict(scope.get('headers', [])).get(b'authorization'))
            await app(scope, receive, send)

        cls.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cls.listener.bind(('127.0.0.1', 0))
        cls.listener.listen(128)
        cls.listener.setblocking(False)
        cls.url = 'http://127.0.0.1:' + str(cls.listener.getsockname()[1]) + '/mcp'
        cls.server = uvicorn.Server(uvicorn.Config(record_headers, host='127.0.0.1', port=0,
                                                  log_level='error', access_log=False))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={'sockets': [cls.listener]}, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 4
        waiter = threading.Event()
        while not cls.server.started and cls.thread.is_alive() and time.monotonic() < deadline:
            waiter.wait(.01)
        if not cls.server.started:
            cls.server.should_exit = True
            cls.thread.join(2)
            cls.listener.close()
            raise RuntimeError('Local MCP fixture failed to start within four seconds')

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(3)
        cls.listener.close()
        if cls.thread.is_alive():
            raise RuntimeError('Local MCP fixture did not stop within three seconds')

    def setUp(self):
        type(self).corrupt_readback = False
        self.client = FastnClient(dict(CONNECTION, gateway_url=self.url))

    def test_sdk_initialize_discover_load_write_and_verified_readback(self):
        discovery = self.client.discover()
        self.assertTrue({'read_context', 'store_report', 'read_report'}.issubset({tool['name'] for tool in discovery['tools']}))
        loaded = self.client.load_brief({'keyword': 'travel mug'}, 'local-fixture-run')
        self.assertEqual(loaded['brief']['keyword'], 'travel mug')
        self.assertTrue(loaded['fixture'])
        payload = {'run_id': 'local-fixture-run', 'brief': {'keyword': 'travel mug'},
                   'report': ANALYSIS, 'evidence': EVIDENCE, 'approved_actions': [ACTION]}
        result = self.client.store_run(payload)
        self.assertEqual(result['status'], 'verified', result)
        self.assertEqual(self.records[payload['run_id']], payload)
        self.assertIn(('Bearer ' + FIXTURE_KEY).encode(), self.authorization_headers)

    def test_matching_run_id_with_different_content_is_not_verified(self):
        type(self).corrupt_readback = True
        payload = {'run_id': 'local-mismatched-run', 'brief': {'keyword': 'travel mug'},
                   'report': ANALYSIS, 'evidence': EVIDENCE, 'approved_actions': []}
        result = self.client.store_run(payload)
        self.assertEqual(result['status'], 'saved_unverified')
        self.assertFalse(result['verified'])
        self.assertIn('did not match', result['verification_error'])

    def test_real_mcp_tool_error_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, 'fixture refusal'):
            self.client.call_tool('rejected_tool', {})


class LocalApiBoundaryTests(unittest.TestCase):
    def setUp(self):
        from MarketRadar.server import make_server
        environment = patch.dict(os.environ, {'MARKETRADAR_FASTN_API_KEY': '', 'MARKETRADAR_FASTN_URL': ''})
        environment.start()
        self.addCleanup(environment.stop)
        self.folder = tempfile.TemporaryDirectory()
        self.radar = Radar(self.folder.name)
        self.radar.settings.save(CONNECTION)
        self.server = make_server(self.radar, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.folder.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def test_settings_response_removes_the_key_and_has_browser_safety_headers(self):
        status, headers, result = self.request('GET', '/api/settings')
        self.assertEqual(status, 200)
        self.assertTrue(result['has_api_key'])
        self.assertNotIn(FIXTURE_KEY, json.dumps(result))
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_untrusted_host_and_origin_cannot_read_or_mutate(self):
        for method, headers in [('GET', {'Host': 'attacker.example'}),
                                ('GET', {'Origin': 'https://attacker.example'}),
                                ('POST', {'Content-Type': 'application/json', 'Origin': 'https://attacker.example'})]:
            with self.subTest(method=method, headers=headers):
                status, _, result = self.request(method, '/api/settings', '{}' if method == 'POST' else None, headers)
                self.assertEqual(status, 403)
                self.assertIn('error', result)
        self.assertEqual(self.radar.settings.get()['read_tool'], 'read_context')

    def test_json_type_syntax_and_size_guards_reject_before_actions(self):
        cases = [('text/plain', '{}', None), ('application/json', '[]', None),
                 ('application/json', '{broken', None), ('application/json', '', '2000001')]
        for content_type, body, length in cases:
            with self.subTest(content_type=content_type, body=body, length=length):
                headers = {'Content-Type': content_type}
                if length:
                    headers['Content-Length'] = length
                status, _, result = self.request('POST', '/api/settings', body, headers)
                self.assertEqual(status, 400)
                self.assertIn('error', result)
        self.assertEqual(self.radar.settings.get()['write_tool'], 'store_report')

    def test_no_arbitrary_files_are_served(self):
        for path in ('/../.env', '/settings.json', '/api/runs/../../.env'):
            status, _, result = self.request('GET', path)
            self.assertIn(status, (400, 404))
            self.assertIn('error', result)
