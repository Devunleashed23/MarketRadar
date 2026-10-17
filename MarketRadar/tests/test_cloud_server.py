"""Real HTTP boundaries for the public demo; no cloud or model requests."""
import base64
from http.client import HTTPConnection
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from MarketRadar.cloud_config import Deployment
from MarketRadar.core import Radar
from MarketRadar.server import make_server


DEMO = 'demo-fixture-password-123'
OWNER = 'owner-fixture-password-456'
PUBLIC = 'https://market-fixture-app.alternatefutures.ai'


class DeploymentTests(unittest.TestCase):
    def test_cloud_requires_https_and_separate_strong_credentials(self):
        for url, demo, owner in [('http://example.com', DEMO, OWNER),
                                 ('https://example.com/path', DEMO, OWNER),
                                 ('https://user:pass@example.com', DEMO, OWNER),
                                 (PUBLIC, 'short', OWNER), (PUBLIC, DEMO, DEMO)]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                Deployment('cloud', url, demo, owner)

    def test_local_network_bind_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, 'network bind'):
                make_server(Radar(folder), 0, '0.0.0.0')

    def test_malformed_credentials_fail_closed(self):
        config = Deployment('cloud', PUBLIC, DEMO, OWNER)
        for header in (None, '', 'Bearer abc', 'Basic !@#$%', 'Basic ' + base64.b64encode(b'owner').decode()):
            self.assertIsNone(config.role(header))


class CloudServerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.radar = Radar(self.folder.name)
        self.server = make_server(self.radar, 0, deployment=Deployment('cloud', PUBLIC, DEMO, OWNER))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.folder.cleanup()

    def request(self, method, path, user=None, password=None, body=None, headers=None):
        supplied = {'Host': PUBLIC.removeprefix('https://')}
        if user:
            supplied['Authorization'] = 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()
        if body is not None:
            supplied['Content-Type'] = 'application/json'
            body = json.dumps(body)
        supplied.update(headers or {})
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        try:
            conn.request(method, path, body=body, headers=supplied)
            response = conn.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), json.loads(data) if data else None
        finally:
            conn.close()

    def test_only_minimal_health_is_public(self):
        status, _, data = self.request('GET', '/healthz')
        self.assertEqual((status, data['status']), (200, 'ok'))
        for path in ('/api/status', '/api/runs'):
            status, headers, _ = self.request('GET', path)
            self.assertEqual(status, 401)
            self.assertIn('Basic realm=', headers['WWW-Authenticate'])

    def test_demo_reads_and_records_review_but_cannot_operate_browser(self):
        status, _, _ = self.request('GET', '/api/runs', 'demo', DEMO)
        self.assertEqual(status, 200)
        for path in ('/api/browser/attach', '/api/browser/open'):
            status, _, _ = self.request('POST', path, 'demo', DEMO, {})
            self.assertEqual(status, 403)
        self.assertEqual(self.request('POST', '/api/runs', 'demo', DEMO, {'keyword': 'travel mug', 'mode': 'live'})[0], 403)
        run = self.radar.start({'keyword': 'travel mug', 'mode': 'sample'})
        self.radar.wait(run['id'])
        status, _, result = self.request('POST', f"/api/runs/{run['id']}/approve", 'demo', DEMO,
                                         {'approved_action_ids': [], 'note': 'Reject speculative actions', 'report_version': 1})
        self.assertEqual(status, 200)
        self.assertEqual(result['approved_action_ids'], [])
        self.assertEqual(self.radar.get(run['id'])['approval']['note'], 'Reject speculative actions')

    def test_owner_only_viewer_auth_and_no_password_leak(self):
        self.assertEqual(self.request('GET', '/api/cloud/browser-auth', 'demo', DEMO)[0], 403)
        self.assertEqual(self.request('GET', '/api/cloud/browser-auth', 'owner', OWNER)[0], 204)
        status, _, result = self.request('GET', '/api/runs', 'owner', OWNER)
        self.assertEqual(status, 200)
        self.assertNotIn(OWNER, json.dumps(result))
        self.assertNotIn(DEMO, json.dumps(result))

    def test_untrusted_hosts_and_origins_are_blocked_even_when_authenticated(self):
        for headers in ({'Host': 'attacker.test'}, {'Origin': 'https://attacker.test'},
                        {'Origin': 'http://market-fixture-app.alternatefutures.ai'}):
            self.assertEqual(self.request('GET', '/api/runs', 'owner', OWNER, headers=headers)[0], 403)

    def test_inference_configuration_is_ready_but_not_claimed_authenticated(self):
        class Model:
            def health(self):
                return {'provider': 'alternate_clouds', 'configured': True, 'authenticated': None, 'available': True}
        with patch('models.get_research_model', return_value=Model()), patch('MarketRadar.browser.BrowserCollector.status', return_value={'connected': False}):
            status, _, result = self.request('GET', '/api/status', 'owner', OWNER)
        self.assertEqual(status, 200)
        self.assertTrue(result['codex']['ready'])
        self.assertFalse(result['codex']['logged_in'])
        self.assertTrue(result['deployment']['browser_viewer'].startswith('/browser/'))


if __name__ == '__main__':
    unittest.main()
