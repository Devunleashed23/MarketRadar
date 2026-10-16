"""Local-only configuration; credential values never appear in API responses."""
import copy
import json
import os
from pathlib import Path
import threading

DEFAULTS = {'gateway_url': 'https://mcp.fastn.dev', 'api_key': '', 'read_tool': '',
            'read_arguments': {'brief': '{brief}'}, 'write_tool': '',
            'write_arguments': {'payload': '{payload}'}, 'verify_tool': '',
            'verify_arguments': {'run_id': '{run_id}'}}


class Settings:
    def __init__(self, directory):
        self.path = Path(directory) / 'settings.json'
        self.lock = threading.RLock()

    def get(self):
        with self.lock:
            result = copy.deepcopy(DEFAULTS)
            if self.path.exists():
                result.update(json.loads(self.path.read_text(encoding='utf-8')))
            if os.getenv('MARKETRADAR_FASTN_API_KEY'):
                result['api_key'] = os.environ['MARKETRADAR_FASTN_API_KEY']
            if os.getenv('MARKETRADAR_FASTN_URL'):
                result['gateway_url'] = os.environ['MARKETRADAR_FASTN_URL']
            return result

    def public(self):
        result = self.get()
        result['has_api_key'] = bool(result.pop('api_key', ''))
        result['configured'] = bool(result['gateway_url'] and result['read_tool'] and result['write_tool'])
        result['url'] = result['gateway_url']
        return result

    def save(self, values):
        with self.lock:
            result = self.get()
            for key, value in values.items():
                if key not in DEFAULTS:
                    continue
                if key.endswith('_arguments'):
                    if isinstance(value, str):
                        value = json.loads(value)
                    if not isinstance(value, dict):
                        raise ValueError('Workflow arguments must be JSON objects.')
                elif not isinstance(value, str) or len(value) > 12000:
                    raise ValueError(f'Invalid connection field: {key}')
                result[key] = value
            from .fastn import FastnClient
            if result.get('gateway_url'):
                FastnClient(result)._url()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(result, indent=2), encoding='utf-8')
            temporary.replace(self.path)
            return self.public()
