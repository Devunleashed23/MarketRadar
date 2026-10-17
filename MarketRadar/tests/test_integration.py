"""Local workflow and HTTP boundaries; no browser or model requests."""
from collections import deque
import copy
from http.client import HTTPConnection
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from MarketRadar.core import Radar, validate_report


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


class WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        # These fixtures cover history and validation, not agent reasoning.
        # A real DeepAgents delegation/tool fixture lives in test_original_agents.
        def research_fixture(brief, model, browser, *, stop=None, emit=None, on_evidence=None, **kwargs):
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
        model = StubModel(responses if responses is not None else [ANALYSIS, {'recommendations': [ACTION]}])
        browser = StubBrowser(EVIDENCE if evidence is None else evidence)
        def model_factory(**kwargs):
            model.emit = kwargs['emit']
            return model
        def browser_factory(**kwargs):
            browser.emit = kwargs['emit']
            return browser
        return Radar(folder, model_factory, browser_factory), model, browser

    def completed_run(self, radar):
        run = radar.start({'keyword': 'travel mug', 'mode': 'live'})
        radar.wait(run['id'], timeout=3)
        self.assertFalse(radar.threads[run['id']].is_alive(), 'The fixture job must finish within its bound')
        return radar.get(run['id'])

    def test_live_pipeline_preserves_partial_evidence_and_human_review(self):
        with tempfile.TemporaryDirectory() as folder:
            radar, model, browser = self.make_radar(folder)
            result = self.completed_run(radar)
            self.assertEqual(result['state'], 'completed', result['error'])
            self.assertEqual(browser.briefs[0]['keyword'], 'travel mug')
            self.assertEqual(len(model.calls), 2)
            self.assertEqual(result['evidence'][0]['data']['keywords'], [])
            self.assertIn('partial evidence', ' '.join(result['report']['limitations']))
            self.assertTrue(all(ref == 'trends-live' for rec in result['report']['recommendations'] for ref in rec['evidence_ids']))
            self.assertTrue(any(event.get('usage') for event in result['events']))
            approval = radar.approve(result['id'], ['review-trends'])
            self.assertEqual(approval['approved_action_ids'], ['review-trends'])
            self.assertEqual(Radar(folder).get(result['id'])['approval'], approval)

    def test_source_failure_fixture_produces_no_metrics_or_recommendations(self):
        failed = [{'id': 'planner', 'source': 'keyword_planner', 'status': 'login_required', 'data': {}},
                  {'id': 'trends', 'source': 'google_trends', 'status': 'unavailable', 'data': {}}]
        with tempfile.TemporaryDirectory() as folder:
            radar, model, _ = self.make_radar(folder, evidence=failed, responses=[])
            result = self.completed_run(radar)
            self.assertEqual(result['state'], 'completed')
            self.assertEqual(result['evidence'], failed)
            self.assertEqual(result['report']['observations'], [])
            self.assertEqual(result['report']['recommendations'], [])
            self.assertEqual(model.calls, [])
            self.assertNotIn('synthetic', json.dumps(result))

    def test_recommendation_cannot_cite_a_failed_source(self):
        unsupported = dict(ACTION, evidence_ids=['planner-blocked'])
        with tempfile.TemporaryDirectory() as folder:
            radar, _, _ = self.make_radar(folder, responses=[ANALYSIS, {'recommendations': [unsupported]}])
            result = self.completed_run(radar)
            self.assertEqual(result['state'], 'failed')
            self.assertIn('source evidence', result['error'])
            self.assertIsNone(result['report'])

    def test_cancelled_collection_does_not_analyze(self):
        entered = threading.Event()
        class BlockingBrowser(StubBrowser):
            def collect(self, brief, model, stop=None):
                entered.set()
                if not stop.wait(2):
                    raise RuntimeError('Fixture cancellation did not arrive in time')
                raise RuntimeError('Fixture collection cancelled')
        with tempfile.TemporaryDirectory() as folder:
            radar, model, _ = self.make_radar(folder)
            radar.browser_factory = lambda **kwargs: BlockingBrowser([], **kwargs)
            run = radar.start({'keyword': 'travel mug', 'mode': 'live'})
            self.assertTrue(entered.wait(1))
            radar.cancel(run['id'])
            radar.wait(run['id'], timeout=3)
            self.assertEqual(radar.get(run['id'])['state'], 'cancelled')
            self.assertEqual(model.calls, [])

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
            radar, model, _ = self.make_radar(folder)
            original = model.complete_json
            def infer(*args, **kwargs):
                model.emit('provider_status', message='Provider progress fixture')
                return original(*args, **kwargs)
            model.complete_json = infer
            result = self.completed_run(radar)
            self.assertEqual(result['state'], 'completed', result['error'])
            self.assertTrue(any(e['message'] == 'Provider progress fixture' for e in result['events']))


class LocalApiBoundaryTests(unittest.TestCase):
    def setUp(self):
        from MarketRadar.server import make_server
        self.folder = tempfile.TemporaryDirectory()
        self.radar = Radar(self.folder.name)
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

    def test_runs_response_has_browser_safety_headers(self):
        status, headers, result = self.request('GET', '/api/runs')
        self.assertEqual(status, 200)
        self.assertEqual(result, {'runs': []})
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_untrusted_host_and_origin_cannot_read_or_mutate(self):
        for method, headers in [('GET', {'Host': 'attacker.example'}),
                                ('GET', {'Origin': 'https://attacker.example'}),
                                ('POST', {'Content-Type': 'application/json', 'Origin': 'https://attacker.example'})]:
            with self.subTest(method=method, headers=headers):
                status, _, result = self.request(method, '/api/runs', '{}' if method == 'POST' else None, headers)
                self.assertEqual(status, 403)
                self.assertIn('error', result)
        self.assertEqual(self.radar.list(), [])

    def test_json_type_syntax_and_size_guards_reject_before_actions(self):
        cases = [('text/plain', '{}', None), ('application/json', '[]', None),
                 ('application/json', '{broken', None), ('application/json', '', '2000001')]
        for content_type, body, length in cases:
            with self.subTest(content_type=content_type, body=body, length=length):
                headers = {'Content-Type': content_type}
                if length:
                    headers['Content-Length'] = length
                status, _, result = self.request('POST', '/api/runs', body, headers)
                self.assertEqual(status, 400)
                self.assertIn('error', result)
        self.assertEqual(self.radar.list(), [])

    def test_no_arbitrary_files_are_served(self):
        for path in ('/../.env', '/settings.json', '/api/runs/../../.env'):
            status, _, result = self.request('GET', path)
            self.assertIn(status, (400, 404))
            self.assertIn('error', result)
