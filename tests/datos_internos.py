#!/usr/bin/env python3
"""Rechaza datos internos de acceso en fleet/ (SEN-7325).

El repositorio es público. Un FQDN de tailnet, una IP de 100.64.0.0/10 o un
home de Linux van con marcadores: <host>.<tailnet>.ts.net, <ip-tailnet-...> y
/home/<usuario>. Las pruebas usan example.ts.net, 100.100.0.0/24 y /home/u.

La salida nombra archivo, línea y tipo, nunca el valor: el log de CI también
es público.
"""
import ipaddress
import re
import subprocess
import sys

SCOPE = 'fleet/'
TAILNET = ipaddress.ip_network('100.64.0.0/10')
SYNTHETIC_NET = ipaddress.ip_network('100.100.0.0/24')
SYNTHETIC_TAILNETS = {'example'}
SYNTHETIC_USERS = {'u'}

FQDN = re.compile(r'([A-Za-z0-9-]+)\.ts\.net(?![A-Za-z0-9-])')
IPV4 = re.compile(r'(?<![0-9.])(\d{1,3}(?:\.\d{1,3}){3})(?![0-9]|\.[0-9]|/[0-9])')
HOME = re.compile(r'(?<![A-Za-z0-9_.~$-])/home/([a-z_][a-z0-9_-]*)(?![a-z0-9_-])')


def tailnet_ip(text):
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return ip in TAILNET and ip not in SYNTHETIC_NET


def findings(text):
    """Devuelve (línea, tipo) por cada dato interno de text."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if any(m[1] not in SYNTHETIC_TAILNETS for m in FQDN.finditer(line)):
            found.append((number, 'FQDN de tailnet'))
        if any(tailnet_ip(m[1]) for m in IPV4.finditer(line)):
            found.append((number, 'IP de tailnet'))
        if any(m[1] not in SYNTHETIC_USERS for m in HOME.finditer(line)):
            found.append((number, 'home de usuario'))
    return found


def main():
    names = subprocess.run(['git', 'ls-files', '-z', '--', SCOPE], check=True,
                           capture_output=True).stdout.split(b'\0')
    total = 0
    for name in filter(None, names):
        path = name.decode('utf-8', 'surrogateescape')
        try:
            with open(path, 'rb') as handle:
                data = handle.read()
        except FileNotFoundError:
            continue
        if b'\0' in data:
            continue
        for number, kind in findings(data.decode('utf-8', 'replace')):
            print(path + ':' + str(number) + ': ' + kind)
            total += 1
    if total:
        print(str(total) + ' dato(s) interno(s) en ' + SCOPE + '; usa los marcadores de tests/datos_internos.py.',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
