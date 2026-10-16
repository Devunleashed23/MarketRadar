"""Real Streamable HTTP MCP calls; tool contracts come from the user's fastn workspace."""
import asyncio
import copy
import json
import re
from datetime import timedelta
from urllib.parse import urlsplit


def expand_template(value, context):
    """Whole-value placeholders preserve dictionaries, arrays, numbers and booleans."""
    if isinstance(value, dict):
        return {key: expand_template(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_template(item, context) for item in value]
    if isinstance(value, str) and re.fullmatch(r'\{[a-z_]+\}', value):
        key = value[1:-1]
        if key not in context:
            raise ValueError(f'Unknown workflow placeholder: {key}')
        return copy.deepcopy(context[key])
    return value


def unwrap(result):
    if isinstance(result, dict):
        if result.get('isError'):
            raise RuntimeError('fastn MCP tool returned an error: ' + json.dumps(result.get('content', []))[:1200])
        if result.get('structuredContent') is not None:
            return result['structuredContent']
        blocks = result.get('content', [])
        parts = [b.get('text', '') for b in blocks if b.get('type') == 'text']
        if parts:
            try:
                return json.loads('\n'.join(parts))
            except ValueError:
                return {'text': '\n'.join(parts)}
    return result


def verify_snapshot(value, payload):
    """A matching ID alone is not evidence that the report was actually stored."""
    if isinstance(value, dict):
        if value.get('run_id') == payload['run_id'] and all(
                value.get(key) == payload[key] for key in ('report', 'evidence', 'approved_actions')):
            return True
        return any(verify_snapshot(item, payload) for item in value.values())
    if isinstance(value, list):
        return any(verify_snapshot(item, payload) for item in value)
    if isinstance(value, str) and value.lstrip().startswith(('{', '[')):
        try:
            return verify_snapshot(json.loads(value), payload)
        except ValueError:
            pass
    return False


class FastnClient:
    def __init__(self, settings, transport=None):
        self.settings = dict(settings)
        self.transport = transport

    def _redact(self, value):
        key = self.settings.get('api_key', '')
        if not key:
            return value
        if isinstance(value, str):
            return value.replace(key, '[redacted]')
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {self._redact(k): self._redact(v) for k, v in value.items()}
        return value

    def _url(self):
        url = self.settings.get('gateway_url', '').strip()
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        fastn_host = any(host == d or host.endswith('.' + d) for d in ('fastn.dev', 'fastn.ai', 'ucl.dev'))
        local = host in ('localhost', '127.0.0.1', '::1')
        if parsed.username or parsed.password or parsed.fragment or not (
                (parsed.scheme == 'https' and fastn_host) or (parsed.scheme in ('http', 'https') and local)):
            raise ValueError('Use the HTTPS gateway copied from fastn, or a localhost MCP test server.')
        return url

    async def _session_request(self, operation, **params):
        try:
            import httpx
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:
            raise RuntimeError('Install MarketRadar/requirements.txt with the project Python to connect fastn MCP.') from exc
        headers = {}
        key = self.settings.get('api_key', '')
        if key:
            headers['Authorization'] = 'Bearer ' + key
        async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(45, connect=20), follow_redirects=True) as http_client, \
                streamable_http_client(self._url(), http_client=http_client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                if operation == 'list_tools':
                    all_tools, cursor = [], None
                    for _ in range(5):
                        result = await session.list_tools(cursor=cursor)
                        all_tools.extend(t.model_dump(mode='json') for t in result.tools)
                        cursor = result.nextCursor
                        if not cursor:
                            break
                    return {'tools': all_tools}
                result = await session.call_tool(params['name'], params['arguments'],
                                                 read_timeout_seconds=timedelta(seconds=45))
                return result.model_dump(mode='json')

    def _request(self, operation, **params):
        self._url()
        try:
            if self.transport:
                return self._redact(self.transport(operation, **params))
            return self._redact(asyncio.run(self._session_request(operation, **params)))
        except Exception as exc:
            message = str(exc)
            if hasattr(exc, 'exceptions'):
                message += ': ' + '; '.join(str(e) for e in exc.exceptions)
            key = self.settings.get('api_key', '')
            if key:
                message = message.replace(key, '[redacted]')
            raise RuntimeError('fastn MCP connection failed: ' + message[:1400]) from None

    def discover(self):
        return self._request('list_tools')

    def call_tool(self, name, arguments):
        if not name or not isinstance(name, str) or len(name) > 200:
            raise ValueError('Choose the actual tool name discovered from your fastn gateway.')
        if not isinstance(arguments, dict):
            raise ValueError('MCP tool arguments must be a JSON object.')
        return unwrap(self._request('call_tool', name=name, arguments=arguments))

    def load_brief(self, brief, run_id='connection-test'):
        context = {'brief': brief, 'brief_id': brief.get('brief_id', brief.get('keyword', '')),
                   'keyword': brief.get('keyword', ''), 'run_id': run_id}
        arguments = expand_template(self.settings.get('read_arguments', {'brief': '{brief}'}), context)
        return self.call_tool(self.settings.get('read_tool', ''), arguments)

    def store_run(self, payload):
        context = dict(payload, payload=payload, keyword=payload['brief']['keyword'])
        arguments = expand_template(self.settings.get('write_arguments', {'payload': '{payload}'}), context)
        result = self.call_tool(self.settings.get('write_tool', ''), arguments)
        verified, verification_error = False, None
        if self.settings.get('verify_tool'):
            try:
                args = expand_template(self.settings.get('verify_arguments', {'run_id': '{run_id}'}), context)
                retrieved = self.call_tool(self.settings['verify_tool'], args)
                verified = verify_snapshot(retrieved, payload)
                if not verified:
                    verification_error = 'Readback did not match the saved report, evidence and approved actions.'
            except Exception as exc:
                verification_error = str(exc)
        return {'status': 'verified' if verified else 'saved_unverified', 'result': result,
                'verified': verified, 'verification_error': verification_error,
                'detail': 'Saved result read back and matched.' if verified else 'Write returned success; persisted content has not been verified.'}
