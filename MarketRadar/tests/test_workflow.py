import tempfile
import unittest
from unittest.mock import Mock, patch

from MarketRadar.core import Radar, validate_brief, validate_report


class WorkflowTests(unittest.TestCase):
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

    def test_sample_never_calls_real_provider_or_fastn_and_cannot_sync(self):
        with tempfile.TemporaryDirectory() as folder:
            radar = Radar(folder)
            run = radar.start({'keyword': 'travel mug', 'mode': 'sample', 'use_fastn': True})
            radar.wait(run['id'], timeout=5)
            result = radar.get(run['id'])
            self.assertEqual(result['state'], 'completed')
            self.assertTrue(all(e['status'] == 'sample' for e in result['evidence']))
            with self.assertRaises(ValueError):
                radar.save(run['id'], [])

    def test_restart_marks_interrupted_runs_instead_of_showing_running_forever(self):
        with tempfile.TemporaryDirectory() as folder:
            radar = Radar(folder)
            run = radar._create(validate_brief({'keyword': 'mug'}), 'live', False)
            other = Radar(folder)
            self.assertEqual(other.get(run['id'])['state'], 'interrupted')

    def test_live_research_defaults_to_no_fastn_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Mock()
            browser = Mock()
            browser.collect.return_value = [{'id': 'blocked', 'source': 'keyword_planner',
                                             'status': 'login_required', 'data': {}}]
            external = Mock(side_effect=AssertionError('Optional fastn must not be constructed.'))
            radar = Radar(folder, model_factory=lambda **kw: model,
                          browser_factory=lambda **kw: browser, fastn_factory=external)
            # This test isolates optional enrichment; the real graph is covered separately.
            outcome = (browser.collect.return_value, {'summary': 'Account attention required.',
                       'observations': [], 'limitations': [], 'recommendations': []})
            with patch('MarketRadar.agents.run_research', return_value=outcome):
                run = radar.start({'keyword': 'travel mug', 'mode': 'live'})
                radar.wait(run['id'])
            result = radar.get(run['id'])
            self.assertFalse(result['use_fastn'])
            self.assertEqual(result['state'], 'completed', result['error'])
            external.assert_not_called()
            model.complete_json.assert_not_called()

    def test_sample_review_persists_without_any_external_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            external = Mock(side_effect=AssertionError('Human review must not call external services.'))
            radar = Radar(folder, model_factory=external, browser_factory=external, fastn_factory=external)
            run = radar.start({'keyword': 'travel mug', 'mode': 'sample'})
            radar.wait(run['id'])
            approval = radar.approve(run['id'], ['sample-next'], 'Use real evidence next.', report_version=1)
            self.assertEqual(approval['approved_action_ids'], ['sample-next'])
            self.assertTrue(approval['sample'])
            self.assertEqual(approval['note'], 'Use real evidence next.')
            self.assertEqual(Radar(folder).get(run['id'])['approval'], approval)
            self.assertEqual(radar.get(run['id'])['sync']['status'], 'not_saved')
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
            queued = radar._create(validate_brief({'keyword': 'travel mug'}), 'live', False)
            with self.assertRaises(ValueError):
                radar.approve(queued['id'], [])
            run = radar.start({'keyword': 'travel mug', 'mode': 'sample'})
            radar.wait(run['id'])
            for ids, note, version in [(['invented'], '', 1), (['sample-next', 'sample-next'], '', 1),
                                       ([], {}, 1), ([], 'x' * 2001, 1), ([], '', 2), ([], '', True)]:
                with self.subTest(ids=ids, version=version), self.assertRaises(ValueError):
                    radar.approve(run['id'], ids, note, report_version=version)
            self.assertIsNone(radar.get(run['id']).get('approval'))

    def test_optional_external_copy_uses_the_recorded_decision_and_never_replaces_it(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Mock()
            client.store_run.return_value = {'status': 'verified'}
            radar = Radar(folder, fastn_factory=lambda settings: client)
            radar.settings.save({'gateway_url': 'http://127.0.0.1:1/mcp',
                                 'read_tool': 'read_fixture', 'write_tool': 'store_fixture'})
            run = radar._create(validate_brief({'keyword': 'travel mug'}), 'live', False)
            action = {'id': 'check-fit', 'title': 'Confirm dimensions', 'reason': 'Check the actual product.',
                      'evidence_ids': ['fixture-source']}
            radar._change(run['id'], state='completed', report={'summary': 'Fixture report',
                          'observations': [], 'limitations': [], 'recommendations': [action]})
            with self.assertRaisesRegex(ValueError, 'Record a human review'):
                radar.save(run['id'], ['check-fit'])
            review = radar.approve(run['id'], ['check-fit'], 'Measure before changing the listing.')
            saved = radar.save(run['id'], ['check-fit'])
            self.assertEqual(client.store_run.call_args.args[0]['human_review'], review)
            self.assertEqual(client.store_run.call_args.args[0]['approved_actions'], [action])
            self.assertEqual(radar.save(run['id'], ['check-fit']), saved)
            client.store_run.assert_called_once()
            radar.approve(run['id'], [], 'Hold all actions until the measurement is available.')
            with self.assertRaisesRegex(ValueError, 'already has an external copy'):
                radar.save(run['id'], [])
            client.store_run.assert_called_once()

