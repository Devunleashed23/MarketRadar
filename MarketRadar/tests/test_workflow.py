import tempfile
import unittest
from unittest.mock import Mock, patch

from MarketRadar.core import Radar, validate_brief, validate_report


class WorkflowTests(unittest.TestCase):
    def test_failed_approved_workflow_can_continue_without_reapproval_or_resending(self):
        with tempfile.TemporaryDirectory() as folder:
            radar = Radar(folder)
            run = radar._create(validate_brief({'keyword': 'mug'}), 'live')
            approved = {'asin': 'B000000001', 'requirements': 'Easy to clean'}
            radar._change(run['id'], state='failed', report={'summary': 'Saved report'},
                          approved_product=approved, error='Inference timed out',
                          review_history=[{'stage': 'approve_product', 'decision': 'approve'}])
            with patch.object(radar, '_execute') as execute:
                radar.begin_review(run['id'])
                radar.wait(run['id'])
                execute.assert_called_once_with(run['id'])
            resumed = radar.get(run['id'])
            self.assertEqual(resumed['approved_product'], approved)
            self.assertIsNone(resumed['error'])
            self.assertEqual(len(resumed['review_history']), 1)
            radar._change(run['id'], state='failed', outreach={'status': 'unknown'})
            with self.assertRaises(ValueError):
                radar.begin_review(run['id'])

    def test_native_review_rejects_stale_or_invalid_approval_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            radar = Radar(folder)
            run = radar._create(validate_brief({'keyword': 'mug'}), 'live')
            request = {'id': 'pending-product', 'action_requests': [{'name': 'approve_product',
                'args': {'product_id': 'B000000001', 'requirements': ''}}]}
            radar._change(run['id'], state='awaiting_review', review_request=request, report={'summary': 'Review'})
            # Waiting for a human is durable, not an abandoned running process.
            radar = Radar(folder)
            self.assertEqual(radar.get(run['id'])['state'], 'awaiting_review')
            for payload in ({'review_id': 'old', 'report_version': 1, 'decision': 'approve'},
                            {'review_id': 'pending-product', 'report_version': 2, 'decision': 'approve'},
                            {'review_id': 'pending-product', 'report_version': 1, 'decision': 'approve'},
                            {'review_id': 'pending-product', 'report_version': 1, 'decision': 'revise', 'feedback': ''}):
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    radar.review(run['id'], payload)

    def test_rejects_invalid_market_and_external_competitors(self):
        with self.assertRaises(ValueError):
            validate_brief({'keyword': 'mug', 'marketplace': 'unknown'})
        with self.assertRaises(ValueError):
            validate_brief({'keyword': 'mug', 'competitor_urls': ['http://127.0.0.1/secrets']})

    def test_recommendations_must_cite_supplied_evidence(self):
        report = {'summary': 'test', 'observations': [], 'limitations': [],
                  'recommendations': [{'id': 'a', 'title': 'test', 'reason': 'test', 'evidence_ids': ['invented']}]}
        with self.assertRaises(ValueError):
            validate_report(report, [{'id': 'real'}])

    def test_sample_never_calls_real_provider_or_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            external = Mock(side_effect=AssertionError('Sample mode must not call external services.'))
            radar = Radar(folder, model_factory=external, browser_factory=external)
            run = radar.start({'keyword': 'travel mug', 'mode': 'sample'})
            radar.wait(run['id'], timeout=5)
            result = radar.get(run['id'])
            self.assertEqual(result['state'], 'completed')
            self.assertTrue(all(e['status'] == 'sample' for e in result['evidence']))
            external.assert_not_called()

    def test_restart_marks_interrupted_runs_instead_of_showing_running_forever(self):
        with tempfile.TemporaryDirectory() as folder:
            radar = Radar(folder)
            run = radar._create(validate_brief({'keyword': 'mug'}), 'live')
            other = Radar(folder)
            self.assertEqual(other.get(run['id'])['state'], 'interrupted')

    def test_sample_review_persists_without_any_external_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            external = Mock(side_effect=AssertionError('Human review must not call external services.'))
            radar = Radar(folder, model_factory=external, browser_factory=external)
            run = radar.start({'keyword': 'travel mug', 'mode': 'sample'})
            radar.wait(run['id'])
            approval = radar.approve(run['id'], ['sample-next'], 'Use real evidence next.', report_version=1)
            self.assertEqual(approval['approved_action_ids'], ['sample-next'])
            self.assertTrue(approval['sample'])
            self.assertEqual(approval['note'], 'Use real evidence next.')
            self.assertEqual(Radar(folder).get(run['id'])['approval'], approval)
            external.assert_not_called()

    def test_human_can_decline_every_action_and_update_their_review(self):
        with tempfile.TemporaryDirectory() as folder:
            radar = Radar(folder)
            run = radar.start({'keyword': 'travel mug', 'mode': 'sample'})
            radar.wait(run['id'])
            first = radar.approve(run['id'], ['sample-next'])
            declined = radar.approve(run['id'], [], 'Not enough evidence to act.')
            self.assertEqual(declined['approved_action_ids'], [])
            self.assertEqual(declined['declined_action_ids'], ['sample-next'])
            self.assertGreater(declined['revision'], first['revision'])
            self.assertEqual(len(radar.get(run['id'])['approval_history']), 2)
            self.assertEqual(radar.approve(run['id'], [], 'Not enough evidence to act.'), declined)
            self.assertEqual(len(radar.get(run['id'])['approval_history']), 2)

    def test_review_rejects_wrong_version_unknown_actions_and_invalid_notes(self):
        with tempfile.TemporaryDirectory() as folder:
            radar = Radar(folder)
            queued = radar._create(validate_brief({'keyword': 'travel mug'}), 'live')
            with self.assertRaises(ValueError):
                radar.approve(queued['id'], [])
            run = radar.start({'keyword': 'travel mug', 'mode': 'sample'})
            radar.wait(run['id'])
            for ids, note, version in [(['invented'], '', 1), (['sample-next', 'sample-next'], '', 1),
                                       ([], {}, 1), ([], 'x' * 2001, 1), ([], '', 2), ([], '', True)]:
                with self.subTest(ids=ids, version=version), self.assertRaises(ValueError):
                    radar.approve(run['id'], ids, note, report_version=version)
            self.assertIsNone(radar.get(run['id']).get('approval'))

