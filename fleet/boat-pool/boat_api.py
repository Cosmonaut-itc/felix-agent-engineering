import json
import os
import subprocess
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class BoatError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BoatError('redirección de Boat rechazada')


def keychain(service):
    try:
        result = subprocess.run(
            ['/usr/bin/security', 'find-generic-password', '-s', service, '-w'],
            capture_output=True, text=True, timeout=10,
            env={'HOME': os.environ['HOME'], 'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
        )
        secret = result.stdout.strip()
        if result.returncode == 0 and secret:
            return secret
    except (OSError, subprocess.SubprocessError, UnicodeError):
        pass
    raise BoatError('llavero no disponible')


def http(method, path, headers, body, timeout):
    data = None if body is None else json.dumps(body).encode()
    request = Request('https://boat.dev/api/v1' + path, data=data, headers=headers, method=method)
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise BoatError('respuesta de Boat demasiado grande')
        return json.loads(raw)


class Boat:
    def __init__(self, org, service, *, transport=http, key_reader=keychain):
        self.org = org
        self.service = service
        self._transport = transport
        self._key_reader = key_reader
        self._key = None

    def _request(self, method, path, body=None):
        if self._key is None:
            self._key = self._key_reader(self.service)
        headers = {
            'Authorization': 'Bearer ' + self._key,
            'X-Boat-Org': self.org,
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }
        try:
            value = self._transport(method, path, headers, body, 10)
        except Exception:
            raise BoatError('petición a Boat no confirmada') from None
        if not isinstance(value, dict) or value.get('ok') is False:
            raise BoatError('respuesta de Boat inválida')
        return value

    def limits(self):
        return self._request('GET', '/limits')

    def find(self, name):
        response = self._request('GET', '/sandboxes?' + urlencode({'name': name, 'limit': 200}))
        sandboxes = response.get('sandboxes')
        page = response.get('pageInfo', {})
        if not isinstance(sandboxes, list) or not isinstance(page, dict) or page.get('hasMore'):
            raise BoatError('lista de Boat incompleta')
        return sandboxes

    def sandbox(self, vm):
        return self._request('GET', '/sandboxes/' + vm.id).get('sandbox')

    def power(self, vm, action):
        if action == 'resume':
            return self._request('POST', '/sandboxes/' + vm.id + '/resume')
        if action == 'stop':
            return self._request('POST', '/sandboxes/' + vm.id + '/stop')
        raise ValueError('acción de energía inválida')

    def renew_lifetime(self, vm, seconds, *, stopped_disk):
        if stopped_disk:
            raise BoatError('Boat no documenta cómo extender la retención de discos detenidos')
        return self._request('PATCH', '/sandboxes/' + vm.id, {'ttlSeconds': seconds}).get('sandbox')

    def key_expiration(self):
        keys = self._request('GET', '/api-keys').get('apiKeys')
        if not isinstance(keys, list):
            raise BoatError('vencimiento de llave sin comprobar')
        matches = [
            item for item in keys
            if isinstance(item, dict)
            and item.get('keyLastFour') == self._key[-4:]
            and isinstance(item.get('keyPrefix'), str)
            and item['keyPrefix']
            and self._key.startswith(item['keyPrefix'])
        ]
        if len(matches) != 1 or 'expiresAt' not in matches[0]:
            raise BoatError('vencimiento de llave sin comprobar')
        return matches[0]['expiresAt']

    def daily_org_cost(self, day):
        # La API pública no publica el total diario de la organización.
        return None
