import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from MarketRadar.browser import BrowserCollector


class ManagedBrowserTests(unittest.TestCase):
    def test_status_does_not_wait_for_a_research_browser_lock(self):
        collector = BrowserCollector(session='status-test')
        acquired, release = threading.Event(), threading.Event()
        def hold():
            with collector._lock:
                acquired.set()
                release.wait(2)
        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(acquired.wait(1))
        try:
            self.assertEqual(collector.status()['status'], 'busy')
        finally:
            release.set()
            worker.join(2)

    def test_shortened_numbers_ranges_and_prices_cannot_become_evidence(self):
        from MarketRadar.browser import _grounded_metric
        for proposed, observed in [('10', '100'), ('9.9', '$19.99'), ('100', '100K'),
                                   ('10', '10 – 100'), ('1K – 10K', '1K – 10K0')]:
            with self.subTest(proposed=proposed, observed=observed):
                self.assertIsNone(_grounded_metric(proposed, observed))
        for observed in ('1K – 10K', '$0.50 – $1.25', '100', 'High'):
            self.assertEqual(_grounded_metric(observed, observed), observed)

    def test_cloud_launch_uses_persistent_sandboxed_chromium_without_extension(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                'MARKETRADAR_BROWSER_MODE': 'headed', 'MARKETRADAR_BROWSER_WORK_DIR': folder,
                'MARKETRADAR_BROWSER_PROFILE': str(Path(folder) / 'profile')}):
            browser = BrowserCollector(session='managed-test')
            with patch.object(browser, '_cli', return_value='ready') as cli:
                self.assertTrue(browser.attach()['connected'])
            args = cli.call_args.args[0]
            self.assertEqual(args[:2], ['open', 'about:blank'])
            self.assertNotIn('--extension=chrome', args)
            config = json.loads(Path(args[-1]).read_text())['browser']
            self.assertFalse(config['launchOptions']['headless'])
            self.assertTrue(config['launchOptions']['chromiumSandbox'])
            self.assertEqual(config['userDataDir'], str(Path(folder) / 'profile'))

    def test_browser_subprocess_does_not_receive_app_model_or_cloud_credentials(self):
        with patch.dict(os.environ, {'MARKETRADAR_BROWSER_MODE': 'headed', 'AF_API_KEY': 'fixture-cloud-key',
                                    'MARKETRADAR_OWNER_PASSWORD': 'fixture-owner', 'OPENAI_API_KEY': 'fixture-openai',
                                    'PLAYWRIGHT_MCP_EXTENSION_TOKEN': 'fixture-extension', 'PATH': '/usr/bin', 'DISPLAY': ':99'}):
            env = BrowserCollector()._environment()
        self.assertEqual(env['DISPLAY'], ':99')
        self.assertEqual(env['PATH'], '/usr/bin')
        self.assertNotIn('AF_API_KEY', env)
        self.assertNotIn('MARKETRADAR_OWNER_PASSWORD', env)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('PLAYWRIGHT_MCP_EXTENSION_TOKEN', env)

    def test_local_extension_keeps_only_its_connection_credential(self):
        with patch.dict(os.environ, {'MARKETRADAR_BROWSER_MODE': 'extension', 'AF_API_KEY': 'fixture-key',
                                    'PLAYWRIGHT_MCP_EXTENSION_TOKEN': 'fixture-extension'}):
            env = BrowserCollector()._environment()
        self.assertEqual(env['PLAYWRIGHT_MCP_EXTENSION_TOKEN'], 'fixture-extension')
        self.assertNotIn('AF_API_KEY', env)


if __name__ == '__main__':
    unittest.main()
