"""Linux container supervisor. Image/runtime smoke tests have not run locally."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from MarketRadar.cloud_config import Deployment


def build_environments(source=None):
    app = dict(os.environ if source is None else source)
    app.update(MARKETRADAR_DEPLOYMENT='cloud', MARKETRADAR_BROWSER_MODE='headed',
               MARKETRADAR_BROWSER_WORK_DIR='/data/browser', MARKETRADAR_BROWSER_PROFILE='/data/profile',
               MARKETRADAR_DATA_DIR='/data/runs', MARKETRADAR_HOST='127.0.0.1', PORT='8877',
               DISPLAY=':99', XDG_RUNTIME_DIR='/data/runtime')
    app.setdefault('MARKETRADAR_PROVIDER', 'alternate_clouds')
    if app['MARKETRADAR_PROVIDER'] not in ('alternate_clouds', 'alternate'):
        raise ValueError('This cloud image requires the explicit alternate_clouds inference provider.')
    Deployment('cloud', app.get('MARKETRADAR_PUBLIC_URL', ''),
               app.get('MARKETRADAR_DEMO_PASSWORD', ''), app.get('MARKETRADAR_OWNER_PASSWORD', ''))
    allowed = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'DISPLAY', 'TMPDIR', 'XDG_RUNTIME_DIR',
               'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'PLAYWRIGHT_BROWSERS_PATH',
               'MARKETRADAR_BROWSER_MODE', 'MARKETRADAR_BROWSER_WORK_DIR', 'MARKETRADAR_BROWSER_PROFILE')
    desktop = {key: app[key] for key in allowed if key in app}
    return app, desktop


def check_children(children):
    for name, process in children:
        if process.poll() is not None:
            raise RuntimeError(f'{name} exited; shutting down the service rather than leaving a partial desktop.')


def wait_ready(check, name, children, stop, timeout=15):
    deadline = time.monotonic() + timeout
    while not stop.is_set() and time.monotonic() < deadline:
        check_children(children)
        if check():
            return
        stop.wait(.1)
    raise RuntimeError(f'{name} did not become ready within its startup bound.')


def port_ready(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.2):
            return True
    except OSError:
        return False


def browser_command(method, environment):
    # Only this child starts/contacts the browser daemon. It never inherits app
    # passwords, provider credentials, or the owner's shell environment.
    script = ('from MarketRadar.browser import BrowserCollector; import sys; '
              f'result=BrowserCollector().{method}(); '
              'print(result.get("message", ""), flush=True) if not result.get("connected") else None; '
              'sys.exit(0 if result.get("connected") else 1)')
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=environment,
                            capture_output=True, timeout=75 if method == 'attach' else 15)
    if result.returncode:
        detail = result.stdout.decode('utf-8', errors='replace').strip()[:1800]
        raise RuntimeError('Managed Chromium could not start or stopped: ' + detail + ' Sandbox remains enabled.')


def main():
    if sys.platform != 'linux' or os.geteuid() == 0:
        raise RuntimeError('Run this Linux image as the non-root marketradar user.')
    app_env, desktop_env = build_environments()
    os.umask(0o077)
    for directory in ('/data/browser', '/data/profile', '/data/runs', '/data/runtime'):
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
            Path(directory).chmod(0o700)
        except PermissionError as exc:
            raise RuntimeError('The mounted /data volume must be writable by UID/GID 10001; prepare ownership on the host.') from exc
    stop, children = threading.Event(), []
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    def start(name, command, environment):
        process = subprocess.Popen(command, cwd=ROOT, env=environment, start_new_session=True)
        children.append((name, process))
        return process

    try:
        start('Xvfb', ['Xvfb', ':99', '-screen', '0', '1365x900x24', '-nolisten', 'tcp', '-ac'], desktop_env)
        wait_ready(lambda: Path('/tmp/.X11-unix/X99').exists(), 'X display', children, stop)
        start('Openbox', ['openbox', '--sm-disable'], desktop_env)
        start('x11vnc', ['x11vnc', '-display', ':99', '-localhost', '-rfbport', '5900',
                         '-nopw', '-forever', '-shared', '-noxdamage', '-quiet'], desktop_env)
        wait_ready(lambda: port_ready(5900), 'Loopback VNC', children, stop)
        start('websockify', ['/usr/bin/websockify', '127.0.0.1:6080', '127.0.0.1:5900'], desktop_env)
        wait_ready(lambda: port_ready(6080), 'Loopback WebSocket bridge', children, stop)
        start('MarketRadar', [sys.executable, '-m', 'MarketRadar', '--host', '127.0.0.1',
                              '--port', '8877', '--data-dir', '/data/runs'], app_env)
        wait_ready(lambda: port_ready(8877), 'Application API', children, stop)
        browser_command('attach', desktop_env)  # Exactly one launch; no fallback or retry.
        start('nginx', ['nginx', '-c', str(ROOT / 'MarketRadar/deploy/nginx.conf')], desktop_env)
        wait_ready(lambda: port_ready(8080), 'Public proxy', children, stop)
        print('MarketRadar proxy ready on port 8080; desktop access requires the owner account.', flush=True)
        next_browser_check = time.monotonic() + 30
        while not stop.wait(.25):
            check_children(children)
            if time.monotonic() >= next_browser_check:
                browser_command('status', desktop_env)
                next_browser_check = time.monotonic() + 30
    finally:
        for _, process in reversed(children):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 5
        for _, process in reversed(children):
            if process.poll() is None:
                try:
                    process.wait(timeout=max(.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('MarketRadar startup failed: ' + str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
