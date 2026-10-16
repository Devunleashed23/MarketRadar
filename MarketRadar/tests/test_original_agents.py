"""Reusable stages in the original agent modules, without legacy credentials."""
import copy
import importlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest


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


if __name__ == '__main__':
    unittest.main()
