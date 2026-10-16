"""Local dashboard and authenticated cloud dashboard behind the bundled proxy."""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from .core import Radar
from .fastn import FastnClient
from .cloud_config import Deployment


def make_server(radar, port=8877, host='127.0.0.1', deployment=None):
    deployment = deployment or Deployment()
    if deployment.mode == 'local' and host not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('A network bind requires authenticated cloud deployment configuration.')
    static = Path(__file__).parent / 'static'
    health_lock, cached_health = threading.Lock(), {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _safe_error(self, exc):
            text = str(exc)[:2500]
            secrets = [radar.settings.get().get('api_key', ''), deployment.owner_password,
                       deployment.demo_password, os.getenv('AF_API_KEY', '')]
            for key in secrets:
                if key:
                    text = text.replace(key, '[redacted]')
            return text

        def _allowed(self):
            expected = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            origins = {'http://' + h for h in expected}
            if deployment.mode == 'cloud':
                expected = {urlsplit(deployment.public_url).netloc}
                origins = {deployment.public_url.rstrip('/')}
            if self.headers.get('Host') not in expected:
                self._json({'error': 'Invalid request host.'}, 403)
                return False
            origin = self.headers.get('Origin')
            if origin and origin not in origins:
                self._json({'error': 'Cross-origin access is blocked.'}, 403)
                return False
            self.role = deployment.role(self.headers.get('Authorization'))
            if not self.role:
                self._json({'error': 'Sign in with the demo or owner account.'}, 401)
                return False
            return True

        def _owner(self):
            if self.role not in ('owner', 'local'):
                self._json({'error': 'This action requires the owner account.'}, 403)
                return False
            return True

        def _headers(self, status, content_type, length):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(length))
            if status == 401:
                self.send_header('WWW-Authenticate', 'Basic realm="MarketRadar", charset="UTF-8"')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()

        def _json(self, value, status=200):
            data = json.dumps(value, ensure_ascii=False).encode('utf-8')
            self._headers(status, 'application/json; charset=utf-8', len(data))
            self.wfile.write(data)

        def _body(self):
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('Send application/json.')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= 2_000_000:
                raise ValueError('Request body is too large.')
            value = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(value, dict):
                raise ValueError('Request must be a JSON object.')
            return value

        def _status(self):
            from models import get_research_model
            from .browser import BrowserCollector
            with health_lock:
                if time.monotonic() - cached_health.get('time', 0) > 15:
                    try:
                        codex = get_research_model().health()
                        codex['logged_in'] = codex.get('authenticated', False)
                        codex['ready'] = bool(codex.get('authenticated') or codex.get('configured'))
                    except Exception as exc:
                        codex = {'available': False, 'logged_in': False, 'detail': self._safe_error(exc)}
                    try:
                        browser = BrowserCollector().status()
                    except Exception as exc:
                        browser = {'available': False, 'detail': self._safe_error(exc)}
                    cached_health.update(time=time.monotonic(), codex=codex, browser=browser)
                public = radar.settings.public()
                requirements = []
                if not cached_health['codex'].get('ready'):
                    requirements.append('Configure Alternate Clouds inference credentials.' if deployment.mode == 'cloud'
                                        else 'Run codex login and sign in with ChatGPT on this computer.')
                if not cached_health['browser'].get('connected'):
                    requirements.append('Start the cloud browser.' if deployment.mode == 'cloud' else 'Attach the Playwright CLI session.')
                requirements.append('Sign in manually to a Google Ads account with Keyword Planner access before live collection.')
                return {'codex': cached_health['codex'], 'browser': cached_health['browser'],
                        'fastn': public, 'requirements': requirements, 'deployment': deployment.public(self.role)}

        def do_GET(self):
            if urlsplit(self.path).path == '/healthz':
                return self._json({'status': 'ok', 'service': 'MarketRadar'})
            if not self._allowed():
                return
            path = urlsplit(self.path).path
            try:
                if path == '/api/cloud/browser-auth':
                    if not self._owner():
                        return
                    self._headers(204, 'text/plain', 0)
                    return
                if path == '/api/status':
                    return self._json(self._status())
                if path == '/api/settings':
                    return self._json(radar.settings.public())
                if path == '/api/runs':
                    return self._json({'runs': radar.list()})
                if path.startswith('/api/runs/'):
                    pieces = path.strip('/').split('/')
                    if len(pieces) not in (3, 4) or (len(pieces) == 4 and pieces[3] != 'export'):
                        raise ValueError('Unknown run endpoint.')
                    return self._json(radar.get(pieces[2]))
                assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/index.html': ('index.html', 'text/html; charset=utf-8'),
                          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/styles.css': ('styles.css', 'text/css; charset=utf-8'),
                          '/static/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/static/styles.css': ('styles.css', 'text/css; charset=utf-8')}
                if path not in assets:
                    return self._json({'error': 'Not found.'}, 404)
                filename, content_type = assets[path]
                data = (static / filename).read_bytes()
                self._headers(200, content_type, len(data))
                self.wfile.write(data)
            except (ValueError, FileNotFoundError) as exc:
                self._json({'error': self._safe_error(exc)}, 400)
            except Exception as exc:
                self._json({'error': self._safe_error(exc)}, 500)

        def do_POST(self):
            if not self._allowed():
                return
            path = urlsplit(self.path).path
            try:
                body = self._body()
                if (path == '/api/settings' or path.startswith('/api/fastn/')
                        or path.startswith('/api/browser/') or path.endswith('/save')) and not self._owner():
                    return
                if path == '/api/settings':
                    return self._json(radar.settings.save(body))
                if path == '/api/fastn/discover':
                    return self._json(FastnClient(radar.settings.get()).discover())
                if path == '/api/fastn/test':
                    # Calls only the explicitly configured loader; never the store tool.
                    brief = body.get('brief', {'keyword': 'connection test', 'country': 'US'})
                    result = FastnClient(radar.settings.get()).load_brief(brief)
                    return self._json({'result': result})
                if path.startswith('/api/browser/'):
                    from .browser import BrowserCollector
                    from .core import validate_brief
                    if any(t.is_alive() for t in radar.threads.values()):
                        raise ValueError('The current research run owns the browser. Cancel it before manual navigation.')
                    browser = BrowserCollector()
                    if path == '/api/browser/attach':
                        result = browser.attach()
                    elif path == '/api/browser/open':
                        source = body.get('source', '')
                        source = 'google_trends' if source == 'trends' else source
                        brief_input = dict(body.get('brief', body))
                        if source == 'keyword_planner' and not brief_input.get('keyword'):
                            # Opening the fixed login page does not run a keyword lookup.
                            brief_input['keyword'] = 'keyword research'
                        result = browser.open_source(source, validate_brief(brief_input))
                    else:
                        raise ValueError('Unknown browser action.')
                    with health_lock:
                        cached_health.clear()
                    return self._json(result)
                if path == '/api/runs':
                    if deployment.mode == 'cloud' and self.role == 'demo' and body.get('mode', 'live') != 'sample':
                        return self._json({'error': 'The owner account runs live research. Demo visitors can explore samples and review saved reports.'}, 403)
                    return self._json(radar.start(body), 202)
                if path.startswith('/api/runs/'):
                    pieces = path.strip('/').split('/')
                    if len(pieces) != 4:
                        raise ValueError('Unknown run action.')
                    identity, action = pieces[2], pieces[3]
                    if action == 'review-start':
                        if not self._owner():
                            return
                        return self._json(radar.begin_review(identity), 202)
                    if action == 'review':
                        if not self._owner():
                            return
                        return self._json(radar.review(identity, body), 202)
                    if action == 'cancel':
                        if self.role == 'demo' and radar.get(identity)['mode'] == 'live':
                            return self._json({'error': 'Only the owner can cancel live research.'}, 403)
                        return self._json(radar.cancel(identity))
                    if action == 'save':
                        return self._json(radar.save(identity, body.get('approved_action_ids', [])))
                    if action == 'approve':
                        return self._json(radar.approve(identity, body.get('approved_action_ids', []),
                                                       body.get('note', ''), report_version=body.get('report_version')))
                self._json({'error': 'Not found.'}, 404)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json({'error': self._safe_error(exc)}, 400)
            except Exception as exc:
                self._json({'error': self._safe_error(exc)}, 500)

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description='Run MarketRadar locally or with explicit cloud authentication.')
    parser.add_argument('--port', type=int, default=int(os.getenv('PORT', '8877')))
    parser.add_argument('--host', default=os.getenv('MARKETRADAR_HOST', '127.0.0.1'))
    parser.add_argument('--data-dir', default=os.getenv('MARKETRADAR_DATA_DIR', str(Path(__file__).parent / '.data')))
    args = parser.parse_args()
    radar = Radar(args.data_dir)
    deployment = Deployment.environment()
    server = make_server(radar, args.port, args.host, deployment)
    print(f'MarketRadar ready ({deployment.mode}) on port {server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for stop in radar.stops.values():
            stop.set()
        server.server_close()


if __name__ == '__main__':
    main()
