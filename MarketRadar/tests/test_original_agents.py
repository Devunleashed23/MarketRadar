"""Reusable stages in the original agent modules, without legacy credentials."""
import copy
import importlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import tempfile
import unittest
from unittest.mock import patch


EVIDENCE = [{'id': 'planner-live', 'source': 'keyword_planner', 'status': 'live',
             'data': {'keywords': [{'keyword': 'travel mug', 'volume': '1K–10K'}]}},
            {'id': 'trends-missing', 'source': 'google_trends', 'status': 'login_required', 'data': {}}]
BRIEF = {'keyword': 'travel mug', 'marketplace': 'amazon', 'country': 'US'}
ANALYSIS = {'summary': 'Observed Planner range; Trends unavailable.', 'observations': ['The observed range is 1K–10K.'],
            'limitations': ['No observed Trends data.'], 'products_in_demand': [],
            'complaints': [], 'requested_features': [], 'market_gaps': []}


class FakeModel:
    def __init__(self, output):
        self.output, self.calls = output, []

    def complete_json(self, system, prompt, **kwargs):
        self.calls.append((system, prompt, kwargs))
        return copy.deepcopy(self.output)


class OriginalAgentStageTests(unittest.TestCase):
    def setUp(self):
        self.research = importlib.import_module('subagents.amazon_market_agent_v2')
        self.listing = importlib.import_module('subagents.listing_agent_v2')

    def test_modules_import_without_site_packages_listing_files_or_google_setup(self):
        project = Path(__file__).resolve().parents[2]
        script = ('import importlib, sys; '
                  'a=importlib.import_module("subagents.amazon_market_agent_v2"); '
                  'b=importlib.import_module("subagents.listing_agent_v2"); '
                  'assert callable(a.analyze_market_evidence) and callable(b.suggest_marketplace_opportunities); '
                  'assert "models" not in sys.modules and "deepagents" not in sys.modules; print("lazy-import-ok")')
        result = subprocess.run([sys.executable, '-S', '-c', script], cwd=project,
                                capture_output=True, text=True, timeout=10,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('lazy-import-ok', result.stdout)

    def test_research_uses_original_dedup_helpers_and_preserves_observed_ranges(self):
        model = FakeModel(ANALYSIS)
        evidence = copy.deepcopy(EVIDENCE)
        result = self.research.analyze_market_evidence(BRIEF, evidence, model)
        self.assertEqual(result, ANALYSIS)
        self.assertEqual(evidence, EVIDENCE)
        policy, prompt, _ = model.calls[0]
        self.assertIn('1K–10K', prompt)
        self.assertIn('planner-live', prompt)
        self.assertIn('untrusted', policy)
        context = self.research.build_research_context(BRIEF, evidence)
        self.assertEqual(context['coverage']['keywords'], 1)

    def test_no_usable_sources_skip_both_model_stages(self):
        failed = [dict(EVIDENCE[1])]
        model = FakeModel({'unexpected': 'must not be called'})
        result = self.research.analyze_market_evidence(BRIEF, failed, model)
        actions = self.listing.suggest_marketplace_opportunities(BRIEF, failed, result, model)
        self.assertEqual(result['observations'], [])
        self.assertEqual(actions, {'recommendations': []})
        self.assertEqual(model.calls, [])

    def test_opportunity_stage_preserves_valid_source_linked_actions(self):
        action = {'id': 'review-mug', 'title': 'Review the Amazon listing wording',
                  'reason': 'Check the observed term against the actual product.', 'evidence_ids': ['planner-live']}
        model = FakeModel({'recommendations': [action]})
        result = self.listing.suggest_marketplace_opportunities(BRIEF, EVIDENCE, ANALYSIS, model)
        self.assertEqual(result, {'recommendations': [action]})
        self.assertIn('amazon', model.calls[0][1])
        self.assertIn('No publishing', model.calls[0][0])

    def test_actions_cannot_cite_missing_failed_or_sample_sources(self):
        for ref in ('invented', 'trends-missing', 'sample-row'):
            with self.subTest(ref=ref):
                model = FakeModel({'recommendations': [{'id': 'a', 'title': 'Review', 'reason': 'Because', 'evidence_ids': [ref]}]})
                evidence = EVIDENCE + [{'id': 'sample-row', 'source': 'marketplace', 'status': 'sample', 'data': {}}]
                with self.assertRaisesRegex(ValueError, 'evidence'):
                    self.listing.suggest_marketplace_opportunities(BRIEF, evidence, ANALYSIS, model)

    def test_invalid_model_shapes_are_rejected(self):
        for output in ({'summary': 3}, {'summary': 'test', 'observations': 'not an array'}):
            with self.subTest(output=output), self.assertRaises(ValueError):
                self.research.analyze_market_evidence(BRIEF, EVIDENCE, FakeModel(output))
        with self.assertRaises(ValueError):
            self.listing.suggest_marketplace_opportunities(BRIEF, EVIDENCE, ANALYSIS, FakeModel({'recommendations': 'wrong'}))

    def test_cancelled_stages_never_call_the_model(self):
        stop = threading.Event()
        stop.set()
        model = FakeModel(ANALYSIS)
        for invoke in (lambda: self.research.analyze_market_evidence(BRIEF, EVIDENCE, model, stop=stop),
                       lambda: self.listing.suggest_marketplace_opportunities(BRIEF, EVIDENCE, ANALYSIS, model, stop=stop)):
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                invoke()
        self.assertEqual(model.calls, [])

    def test_large_context_is_structurally_bounded_and_omissions_are_visible(self):
        rows = [{'date': f'2026-01-{i % 28 + 1:02d}', 'value': str(i % 101), 'term': 'mug' * 30} for i in range(1500)]
        evidence = [{'id': 'trends-live', 'source': 'google_trends', 'status': 'live', 'data': {'trends': rows}, 'raw_excerpt': 'x' * 50000}]
        original = copy.deepcopy(evidence)
        context = self.research.build_research_context(BRIEF, evidence)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 65000)
        self.assertTrue(context['omissions'])
        self.assertEqual(evidence, original)


class DeepAgentWorkflowTests(unittest.TestCase):
    def test_product_feedback_recollects_sources_and_reaches_a_new_review(self):
        from langgraph.checkpoint.sqlite import SqliteSaver
        from MarketRadar.agents import run_research
        product = {'asin': 'B000000001', 'title': 'Fixture bottle'}
        evidence = copy.deepcopy(EVIDENCE) + [{'id': 'amazon-research', 'source': 'marketplace',
            'status': 'live', 'data': {'products': [product]}}]
        session = {'id': 'feedback-fixture', 'report': dict(ANALYSIS), 'evidence': evidence}
        choices = [('approve_product', {'product_id': product['asin'], 'requirements': ''}),
            ('revise_research', {}), ('task', {'subagent_type': 'google-demand', 'description': 'Research easy-clean bottles.'}),
            ('open_google', {'source': 'keyword_planner'}), ('open_google', {'source': 'google_trends'}),
            (None, 'Google attempted.'), ('task', {'subagent_type': 'amazon-reviews', 'description': 'Research easy-clean bottles.'}),
            ('research_amazon', {'searches': []}), (None, 'Products collected.'), ('read_evidence', {}),
            ('finalize_report', {'report': dict(ANALYSIS, recommendations=[])}),
            ('approve_product', {'product_id': product['asin'], 'requirements': 'Easy to clean'})]
        class Provider:
            model, turn = 'fixture', 0
            def complete_json(self, system, prompt, **kwargs):
                name, args = choices[self.turn]
                self.turn += 1
                if self.turn > 1:
                    assert 'easy-clean' in prompt
                return {'content': '' if name else args, 'tool_calls': [] if not name else [
                    {'name': name, 'args': json.dumps(args), 'id': f'feedback-{self.turn}'}]}
        class Browser:
            keywords = []
            def open_google(self, source, brief, stop=None):
                self.keywords.append(brief['keyword'])
                return {'evidence': next(row for row in evidence if row['source'] == source)}
            def collect_amazon(self, brief, searches, stop=None):
                self.keywords.append(brief['keyword'])
                return evidence[-1]
        provider, browser = Provider(), Browser()
        with tempfile.TemporaryDirectory() as folder:
            with SqliteSaver.from_conn_string(str(Path(folder) / 'checkpoint.sqlite')) as saver:
                run_research(BRIEF, provider, browser, session=session, checkpointer=saver)
                session.update(revision_requested=True, human_feedback='Focus on easy-clean bottles.')
                run_research(dict(BRIEF, keyword='easy-clean bottle'), provider, browser, session=session,
                    checkpointer=saver, resume={'decisions': [{'type': 'reject', 'message': session['human_feedback']}]})
            self.assertEqual(browser.keywords, ['easy-clean bottle'] * 3)
            self.assertEqual(session['state'], 'awaiting_review')
            self.assertEqual(session['review_request']['action_requests'][0]['args']['requirements'], 'Easy to clean')

    def test_native_review_resumes_after_restart_and_never_contacts_before_approval(self):
        from langgraph.checkpoint.sqlite import SqliteSaver
        from MarketRadar.agents import run_research
        product = {'asin': 'B000000001', 'title': 'Fixture mug', 'url': 'https://www.amazon.com/dp/B000000001'}
        session = {'id': 'review-fixture', 'report': dict(ANALYSIS), 'evidence': copy.deepcopy(EVIDENCE) + [
            {'id': 'amazon-research', 'source': 'marketplace', 'status': 'live', 'data': {'products': [product]}}]}
        draft = {'supplier_id': 'supplier-1', 'subject': 'Sample inquiry', 'body': 'Can you provide a leak-resistant lid sample?'}

        class Provider:
            model = 'fixture'
            turn = 0
            def complete_json(self, system, prompt, **kwargs):
                sequence = [('approve_product', {'product_id': product['asin'], 'requirements': 'Leak-resistant lid'}),
                    ('task', {'subagent_type': 'supplier-scout', 'description': 'Check suppliers for the approved mug.'}),
                    ('find_suppliers', {}), (None, 'Supplier profile observed; samples need checking.'),
                    ('contact_supplier', draft), ('contact_supplier', dict(draft, body='Please quote a sample and show your audit report.'))]
                name, args = sequence[self.turn]
                self.turn += 1
                if name is None:
                    return {'content': args, 'tool_calls': []}
                return {'content': '', 'tool_calls': [{'name': name, 'args': json.dumps(args), 'id': f'review-{self.turn}'}]}

        class Browser:
            calls = 0
            def collect_suppliers(self, brief, choice, stop=None):
                self.calls += 1
                assert choice['requirements'] == 'Leak-resistant lid'
                return {'status': 'live', 'notes': [], 'suppliers': [{'id': 'supplier-1', 'name': 'Fixture supplier',
                    'url': 'https://fixture.en.made-in-china.com/', 'email': 'sales@example.com'}]}

        provider, browser = Provider(), Browser()
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'MARKETRADAR_SMTP_HOST': ''}):
            def invoke(resume=None):
                # Close/reopen the DB and reconstruct the agent as a server restart would.
                with SqliteSaver.from_conn_string(str(Path(folder) / 'checkpoints.sqlite')) as saver:
                    return run_research(BRIEF, provider, browser, session=session, checkpointer=saver, resume=resume)
            invoke()
            self.assertEqual(session['state'], 'awaiting_review')
            self.assertEqual(session['review_request']['action_requests'][0]['name'], 'approve_product')
            self.assertEqual(browser.calls, 0)
            invoke({'decisions': [{'type': 'approve'}]})
            self.assertEqual(browser.calls, 1)
            self.assertNotIn('outreach', session)
            self.assertEqual(session['review_request']['action_requests'][0]['name'], 'contact_supplier')
            session['human_feedback'] = 'Ask for the audit report before discussing quantities.'
            invoke({'decisions': [{'type': 'reject', 'message': session['human_feedback']}]})
            self.assertNotIn('outreach', session)
            self.assertIn('audit report', session['review_request']['action_requests'][0]['args']['body'])
            invoke({'decisions': [{'type': 'edit', 'edited_action': {'name': 'contact_supplier',
                'args': dict(draft, body='Human-edited sample request.')}}]})
            self.assertEqual(session['outreach']['status'], 'approved_draft')
            self.assertEqual(session['outreach']['body'], 'Human-edited sample request.')
            self.assertTrue(session['closed'])
            self.assertEqual(provider.turn, 6)

    def test_real_graph_delegates_executes_tools_and_validates_citations(self):
        from MarketRadar.agents import run_research
        from MarketRadar.browser import BrowserCollector

        planner = copy.deepcopy(EVIDENCE[0])
        trends = {'id': 'trends-live', 'source': 'google_trends', 'status': 'live', 'data': {'trends': []}}
        amazon = {'id': 'amazon-research', 'source': 'marketplace', 'status': 'live',
                  'data': {'products': [{'asin': 'B000000001', 'title': 'Fixture mug', 'reviews': [
                      {'id': 'R-FIXTURE', 'body': 'The lid leaks.'}]}]}}
        report = dict(ANALYSIS, complaints=[{'title': 'Leaking lid', 'detail': 'One fixture buyer reports leakage.',
            'evidence_ids': ['amazon-research'], 'review_ids': ['R-FIXTURE']}], recommendations=[])

        class ScriptedProvider:
            model = 'fixture'

            def __init__(self):
                self.turn = 0
                self.requests = []

            def complete_json(self, system, prompt, **kwargs):
                request = json.loads(prompt)
                self.requests.append(request)
                sequence = [
                    ('task', {'subagent_type': 'google-demand', 'description': 'Collect both Google sources.'}),
                    ('open_google', {'source': 'keyword_planner'}),
                    ('google_step', {'decision': {'action': 'done', 'data': planner['data']}}),
                    ('open_google', {'source': 'google_trends'}),
                    (None, 'Google collection complete.'),
                    ('task', {'subagent_type': 'amazon-reviews', 'description': 'Research mug reviews.'}),
                    ('research_amazon', {'searches': []}),
                    (None, 'Fixture review R-FIXTURE reports leakage.'),
                    ('read_evidence', {}),
                    ('finalize_report', {'report': dict(report, recommendations=[{
                        'id': 'bad', 'title': 'Bad reference', 'reason': 'Fixture correction', 'evidence_ids': ['missing-source']}])}),
                    ('finalize_report', {'report': report})]
                name, args = sequence[self.turn]
                self.turn += 1
                available = {entry['function']['name'] for entry in request['available_tools']}
                assert not available.intersection({'execute', 'read_file', 'write_file', 'ls', 'grep'})
                # Reproduce the live failure: after Google, prefer reading early
                # whenever the orchestrator still exposes that premature action.
                if self.turn == 6 and 'read_evidence' in available:
                    name, args = 'read_evidence', {}
                if name is None:
                    return {'content': args, 'tool_calls': []}
                assert name in available, (name, available)
                return {'content': '', 'tool_calls': [{'name': name, 'args': json.dumps(args), 'id': f'fixture-{self.turn}'}]}

        class FixtureBrowser:
            def open_google(self, source, brief, stop=None):
                if source == 'google_trends':
                    return {'source': source, 'status': 'live', 'evidence': trends}
                return {'source': source, 'status': 'running', 'snapshot': 'Fixture keyword row', 'evidence': None}

            def google_step(self, decision, stop=None):
                assert decision['action'] == 'done'
                return {'source': 'keyword_planner', 'status': 'live', 'evidence': planner}

            def collect_amazon(self, brief, searches, stop=None):
                assert searches == []
                return amazon

        provider = ScriptedProvider()
        evidence, output = run_research(BRIEF, provider, FixtureBrowser())
        self.assertEqual(provider.turn, 11)  # Correct one citation, then exit without another model call.
        self.assertEqual({row['id'] for row in evidence}, {'planner-live', 'trends-live', 'amazon-research'})
        self.assertEqual(output['complaints'][0]['review_ids'], ['R-FIXTURE'])
        for index, required in ((0, 'task'), (5, 'task'), (8, 'read_evidence'),
                                (9, 'finalize_report'), (10, 'finalize_report')):
            request = provider.requests[index]
            self.assertEqual([entry['function']['name'] for entry in request['available_tools']], [required])
            self.assertEqual(request['tool_choice'], required)
        self.assertFalse(hasattr(BrowserCollector, '_collect_source'))


if __name__ == '__main__':
    unittest.main()
