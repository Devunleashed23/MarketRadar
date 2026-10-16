"""Explicit deployment boundaries; local mode never opens a network interface."""
import base64
import binascii
from dataclasses import dataclass
import hmac
import os
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Deployment:
    mode: str = 'local'
    public_url: str = ''
    demo_password: str = ''
    owner_password: str = ''

    def __post_init__(self):
        if self.mode not in ('local', 'cloud'):
            raise ValueError('MARKETRADAR_DEPLOYMENT must be local or cloud.')
        if self.mode == 'cloud':
            url = urlsplit(self.public_url)
            if (url.scheme != 'https' or not url.hostname or url.username or url.password
                    or url.path not in ('', '/') or url.query or url.fragment or url.port not in (None, 443)):
                raise ValueError('Cloud mode requires an HTTPS MARKETRADAR_PUBLIC_URL without a path.')
            if any(len(value) < 16 or len(value) > 256 or ':' in value or '\n' in value
                   for value in (self.demo_password, self.owner_password)):
                raise ValueError('Set separate demo and owner passwords of 16–256 characters.')
            if hmac.compare_digest(self.demo_password.encode(), self.owner_password.encode()):
                raise ValueError('Owner and demo passwords must differ.')

    @classmethod
    def environment(cls):
        return cls(os.getenv('MARKETRADAR_DEPLOYMENT', 'local'),
                   os.getenv('MARKETRADAR_PUBLIC_URL', '').rstrip('/'),
                   os.getenv('MARKETRADAR_DEMO_PASSWORD', ''),
                   os.getenv('MARKETRADAR_OWNER_PASSWORD', ''))

    def role(self, authorization):
        if self.mode == 'local':
            return 'local'
        try:
            scheme, encoded = (authorization or '').split(' ', 1)
            if scheme.lower() != 'basic' or len(encoded) > 1024:
                return None
            name, password = base64.b64decode(encoded, validate=True).decode('utf-8').split(':', 1)
        except (ValueError, UnicodeError, binascii.Error):
            return None
        for username, secret in (('owner', self.owner_password), ('demo', self.demo_password)):
            if hmac.compare_digest(name.encode(), username.encode()) and hmac.compare_digest(password.encode(), secret.encode()):
                return username
        return None

    def public(self, role):
        return {'mode': self.mode, 'role': role,
                'browser_viewer': '/browser/vnc.html#autoconnect=1&resize=scale&path=browser/websockify'
                if self.mode == 'cloud' and role == 'owner' else None}
