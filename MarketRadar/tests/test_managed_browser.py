import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from MarketRadar.browser import BrowserCollector


class ManagedBrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'The Playwright CLI requires Node.js.')
    def test_amazon_recognizes_current_review_section_and_signin_requirement(self):
        harness = r'''
const fs = require('fs'), vm = require('vm'); let scrolled = false;
const section = {innerText: 'Customer reviews\nSign in to see customer reviews.'};
const document = {querySelector: s => s === '#productTitle' ? {innerText:'Water bottle sleeve'} :
  s.includes('customer-reviews_feature_div') ? section : null, querySelectorAll: () => []};
const script = fs.readFileSync(process.argv[1], 'utf8').replace('/*MARKETRADAR_CONFIG*/ {}',
  JSON.stringify({action:'product',asin:'B09PY5G4BW'}));
const collect = vm.runInNewContext('(' + script + ')', {document});
const page = {goto: async () => ({status:()=>200}), url:()=> 'https://www.amazon.com/dp/B09PY5G4BW',
  evaluate:async (fn,arg)=>fn(arg), locator:s=>({first(){return this}, waitFor:async()=>{},
    count:async()=>s.includes('customer-reviews_feature_div') ? 1 : 0,
    scrollIntoViewIfNeeded:async()=>{scrolled=true}, innerText:async()=>section.innerText})};
collect(page).then(result=>console.log(JSON.stringify({result,scrolled})));
'''
        script = Path(__file__).parents[1] / 'amazon_research.js'
        completed = subprocess.run([shutil.which('node'), '-e', harness, str(script)],
                                   capture_output=True, text=True, check=True, timeout=5)
        observed = json.loads(completed.stdout)
        self.assertTrue(observed['scrolled'])
        self.assertEqual(observed['result']['products'][0]['review_status'], 'login_required')
        self.assertEqual(observed['result']['products'][0]['reviews'], [])
        self.assertIn('sign in', ' '.join(observed['result']['notes']).lower())

    def test_managed_launch_keeps_plain_cli_errors_without_account_urls(self):
        with patch.dict(os.environ, {'MARKETRADAR_BROWSER_MODE': 'headed'}):
            browser = BrowserCollector()
        with patch('MarketRadar.browser.shutil.which', return_value='node'), patch.object(Path, 'is_file', return_value=True), \
                patch('MarketRadar.browser.subprocess.Popen') as launch:
            launch.return_value.returncode = 1
            launch.return_value.communicate.return_value = ('FATAL: fixture namespace failure https://example.com/?token=hidden Bearer secret-value\n' + 'stack frame\n' * 500, None)
            with self.assertRaisesRegex(RuntimeError, 'fixture namespace failure') as raised:
                browser._cli(['open', 'about:blank'])
            self.assertNotIn('secret-value', str(raised.exception))
            self.assertNotIn('example.com', str(raised.exception))

    @unittest.skipUnless(shutil.which('node'), 'The Playwright CLI requires Node.js.')
    def test_supplier_script_runs_without_url_global_and_restricts_hosts(self):
        # Playwright CLI run-code uses a VM without the browser's URL global.
        harness = r'''
const fs = require('fs'), vm = require('vm');
const document = {
  querySelector: s => s === '.shop-sign__as img[alt="Audited Supplier"]' ? {alt: 'Audited Supplier', getClientRects: () => [{}]} : null,
  querySelectorAll: s => s === 'a[href]' ? [{pathname: '/contact-info.html', href: process.argv[2]}] :
    s === 'a[href^="mailto:"]' ? [{textContent: 'sales@maker.example', getAttribute: () => 'mailto:sales@maker.example'}] : []
};
const collect = vm.runInNewContext('(' + fs.readFileSync(process.argv[1], 'utf8') + ')', {document});
let current = ''; const visited = [];
const row = url => ({innerText: '500 pieces MOQ', querySelector: selector =>
  selector.split(',').map(s => s.trim()).includes('.company-name-txt a') ? {innerText: 'Fixture manufacturer', href: url} :
  selector === '.product-name' ? {innerText: 'Steel bottle'} : null});
const rows = [row('https://maker.en.made-in-china.com.evil.test/'), row('https://evil.test/'),
  row('https://maker.en.made-in-china.com:443/'), row('https://user@maker.en.made-in-china.com/'),
  row('https://maker.en.made-in-china.com/?ads_tp=ppc&ads_id=fixture'), row('https://maker.en.made-in-china.com/')];
const page = {
  goto: async url => { current = url; visited.push(url); return {status: () => 200}; }, url: () => current,
  locator: selector => { if (selector !== 'body' && !selector.split(',').map(s => s.trim()).includes('.list-node')) throw Error('Missing observed list-node layout');
    return {innerText: async () => 'Public supplier profile', first() { return this; },
      waitFor: async () => {}, evaluateAll: async fn => fn(rows)}; },
  evaluate: async fn => fn()
};
collect(page).then(result => console.log(JSON.stringify({result, visited}))).catch(error => {console.error(error); process.exit(1);});
'''
        for contact, expected in [('https://maker.en.made-in-china.com/contact-info.html?from=profile&source=research', True),
                                  ('https://maker.en.made-in-china.com.evil.test/contact-info.html', False)]:
            with self.subTest(contact=contact), tempfile.TemporaryDirectory() as folder:
                browser = BrowserCollector(session='supplier-vm-test')
                browser.root = Path(folder)
                def cli(args, **_kwargs):
                    if args[0] != 'run-code':
                        return ''
                    script = args[1].split('=', 1)[1]
                    completed = subprocess.run([shutil.which('node'), '-e', harness, script, contact],
                        capture_output=True, text=True, check=True, timeout=10)
                    observed = json.loads(completed.stdout)
                    self.assertEqual(len(observed['visited']), 2)  # Search and one safe, unique profile.
                    return '### Result\n' + json.dumps(observed['result'])
                with patch.object(browser, '_cli', side_effect=cli):
                    result = browser.collect_suppliers({'keyword': 'water bottle'}, {'title': 'Steel bottle'})
                self.assertEqual(result['status'], 'live', result['notes'])
                self.assertEqual(len(result['suppliers']), 1)
                supplier = result['suppliers'][0]
                self.assertIn('Audited Supplier', supplier['verification'])
                self.assertEqual(supplier['contact_url'], contact if expected else None)
                self.assertFalse(list(Path(folder).rglob('suppliers-*.js')))

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
