"""Run against a local Docker publish; no account/browser research or inference."""
import base64
from http.client import HTTPConnection
import json
import os
import time


HOST = 'ci.marketradar.example'


def request(path, username=None, *, websocket=False, method='GET', body=None):
    connection = HTTPConnection('127.0.0.1', 8080, timeout=5)
    headers = {'Host': HOST}
    if username:
        password = os.environ['MARKETRADAR_' + username.upper() + '_PASSWORD']
        token = base64.b64encode((username + ':' + password).encode()).decode()
        headers['Authorization'] = 'Basic ' + token
    if body is not None:
        headers['Content-Type'] = 'application/json'
    if websocket:
        headers.update(Upgrade='websocket', Connection='Upgrade',
                       **{'Sec-WebSocket-Version': '13', 'Sec-WebSocket-Key': 'bWFya2V0cmFkYXJzbW9rZQ=='})
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        return response.status, b'' if response.status == 101 else response.read()
    finally:
        connection.close()


def main():
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            if request('/healthz')[0] == 200:
                break
        except OSError:
            pass
        time.sleep(.25)
    else:
        raise RuntimeError('Container proxy did not become healthy within 120 seconds')
    for path in ('/', '/browser/vnc.html', '/browser/websockify'):
        assert request(path)[0] == 401, (path, 'unauthenticated access was not denied')
    assert request('/', 'demo')[0] == 200
    assert request('/browser/vnc.html', 'demo')[0] == 403
    assert request('/browser/websockify', 'demo', websocket=True)[0] == 403
    assert request('/browser/vnc.html', 'owner')[0] == 200
    assert request('/browser/websockify', 'owner', websocket=True)[0] == 101
    assert request('/api/settings', 'demo', method='POST', body='{}')[0] == 403
    live = json.dumps({'keyword': 'blocked demo inference', 'mode': 'live', 'use_fastn': False})
    assert request('/api/runs', 'demo', method='POST', body=live)[0] == 403
    code, body = request('/api/settings', 'owner')
    assert code == 200
    settings = json.loads(body)
    assert 'api_key' not in settings
    sample = json.dumps({'keyword': 'container smoke mug', 'mode': 'sample', 'use_fastn': False})
    code, body = request('/api/runs', 'demo', method='POST', body=sample)
    assert code == 202, body
    identity = json.loads(body)['id']
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        _, body = request('/api/runs/' + identity, 'demo')
        if json.loads(body)['state'] == 'completed':
            break
        time.sleep(.1)
    else:
        raise RuntimeError('Synthetic sample did not complete')
    print('Container health, demo/owner boundaries, VNC WebSocket auth, and synthetic workflow passed.')


if __name__ == '__main__':
    main()
