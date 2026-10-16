"""Host-independent checks; container integration is delegated to CI."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import subprocess
import tempfile


class BootstrapTests(unittest.TestCase):
    def test_startup_failure_reads_only_latest_daemon_tail_and_redacts_diagnostics(self):
        module = self.bootstrap()
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / '.cache/ms-playwright/daemon'
            old = cache / 'old/marketradar.err'
            latest = cache / 'latest/marketradar.err'
            old.parent.mkdir(parents=True)
            latest.parent.mkdir(parents=True)
            old.write_text('old launch failure', encoding='utf-8')
            module.os.utime(old, (1, 1))
            latest.write_text('ignored prefix ' + 'x' * 7000 + '\nnamespace EPERM\n'
                              'https://private.example/?token=secret-url\nowner@example.com\n'
                              'Bearer secret-bearer\nAF_API_KEY=secret-key\n', encoding='utf-8')
            failure = subprocess.CompletedProcess([], 1, stdout=b'generic CLI failure',
                                                  stderr=b'node setup failure SECRET=secret-stderr')
            with patch.object(module.subprocess, 'run', return_value=failure) as run:
                with self.assertRaises(RuntimeError) as caught:
                    module.browser_command('attach', {'HOME': directory})
            detail = str(caught.exception)
            self.assertIn('namespace EPERM', detail)
            self.assertIn('node setup failure', detail)
            self.assertIn('Sandbox remains enabled', detail)
            for hidden in ('old launch failure', 'ignored prefix', 'private.example', 'owner@example.com',
                           'secret-url', 'secret-bearer', 'secret-key', 'secret-stderr'):
                self.assertNotIn(hidden, detail)
            self.assertLess(len(detail), 5000)
            run.assert_called_once()

    def test_browser_startup_keeps_the_actual_sanitized_failure_reason(self):
        module = self.bootstrap()
        failure = subprocess.CompletedProcess([], 1, stdout=b'Playwright CLI: fixture launch failure', stderr=b'')
        with patch.object(module.subprocess, 'run', return_value=failure):
            with self.assertRaisesRegex(RuntimeError, 'fixture launch failure.*Sandbox remains enabled'):
                module.browser_command('attach', {})

    def bootstrap(self):
        spec = importlib.util.spec_from_file_location('radar_bootstrap', Path(__file__).with_name('bootstrap.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_browser_and_desktop_children_receive_no_provider_or_account_secrets(self):
        module = self.bootstrap()
        source = {'PATH': '/usr/bin', 'HOME': '/home/marketradar', 'AF_API_KEY': 'fixture-provider-secret',
                  'AF_ORG_ID': 'fixture-org', 'OPENAI_API_KEY': 'fixture-other-secret',
                  'MARKETRADAR_PUBLIC_URL': 'https://fixture.example',
                  'MARKETRADAR_OWNER_PASSWORD': 'fixture-owner-password-long',
                  'MARKETRADAR_DEMO_PASSWORD': 'fixture-demo-password-long'}
        app, desktop = module.build_environments(source)
        self.assertEqual(app['AF_API_KEY'], source['AF_API_KEY'])
        self.assertEqual(app['MARKETRADAR_DEPLOYMENT'], 'cloud')
        self.assertEqual(app['MARKETRADAR_BROWSER_MODE'], 'headed')
        self.assertEqual(app['MARKETRADAR_BROWSER_WORK_DIR'], '/data/browser')
        self.assertEqual(app['MARKETRADAR_BROWSER_PROFILE'], '/data/profile')
        for key in ('AF_API_KEY', 'AF_ORG_ID', 'OPENAI_API_KEY', 'MARKETRADAR_OWNER_PASSWORD', 'MARKETRADAR_DEMO_PASSWORD'):
            self.assertNotIn(key, desktop)
        self.assertEqual(desktop['DISPLAY'], ':99')

    def test_missing_or_equal_owner_demo_secrets_fail_before_startup(self):
        module = self.bootstrap()
        with self.assertRaises(ValueError):
            module.build_environments({})
        source = {'MARKETRADAR_PUBLIC_URL': 'https://fixture.example',
                  'MARKETRADAR_OWNER_PASSWORD': 'same-fixture-password',
                  'MARKETRADAR_DEMO_PASSWORD': 'same-fixture-password'}
        with self.assertRaises(ValueError):
            module.build_environments(source)

    def test_any_supervised_child_exit_fails_the_service(self):
        module = self.bootstrap()
        class Process:
            def poll(self):
                return 0
        with self.assertRaisesRegex(RuntimeError, 'desktop'):
            module.check_children([('desktop', Process())])

    def test_nginx_protects_viewer_and_websocket_using_the_real_auth_context(self):
        text = Path(__file__).with_name('nginx.conf').read_text(encoding='utf-8')
        self.assertIn('listen 8080;', text)
        self.assertIn('location = /browser/websockify', text)
        self.assertIn('location /browser/', text)
        self.assertGreaterEqual(text.count('auth_request /_browser_auth;'), 2)
        self.assertIn('proxy_set_header Host $http_host;', text)
        self.assertIn('proxy_set_header Authorization $http_authorization;', text)
        self.assertIn('proxy_pass http://127.0.0.1:8877/api/cloud/browser-auth;', text)


if __name__ == '__main__':
    unittest.main()
