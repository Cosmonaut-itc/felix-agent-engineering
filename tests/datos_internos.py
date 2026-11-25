#!/usr/bin/env python3
"""Rechaza datos internos de acceso en fleet/ y scripts/.

El repositorio es público. Un FQDN de tailnet, una IP de 100.64.0.0/10 o un
home de Linux, macOS o Windows van con marcadores: <host>.<tailnet>.ts.net,
<ip-tailnet-...>, <home>, /home/<usuario>, /c/Users/<usuario> y
`wsl -u <usuario>`. Las pruebas usan example.ts.net, 100.100.0.0/24,
la cuenta u y los demás nombres de SYNTHETIC_USERS.

La salida nombra archivo, línea y tipo, nunca el valor: el log de CI también
es público.
"""
import ipaddress
import re
import subprocess
import sys

SCOPE = ('fleet/', 'scripts/')
TAILNET = ipaddress.ip_network('100.64.0.0/10')
SYNTHETIC_NET = ipaddress.ip_network('100.100.0.0/24')
SYNTHETIC_TAILNETS = {'example'}
SYNTHETIC_USERS = {'u', 'root'}
MAC_ALLOWED_USERS = SYNTHETIC_USERS | {'shared'}
WINDOWS_ALLOWED_USERS = SYNTHETIC_USERS | {'public'}

FQDN = re.compile(r'([A-Za-z0-9-]+)\.ts\.net(?![A-Za-z0-9-])', re.IGNORECASE)
IPV4 = re.compile(r'(?<![0-9.])(\d{1,3}(?:\.\d{1,3}){3})(?:/(\d{1,2}))?(?![0-9]|\.[0-9])')
HOME = re.compile(r'(?<![A-Za-z0-9_.~$-])/home/([a-z_][a-z0-9_-]*)(?![a-z0-9_-])')
WINDOWS_LOCATION = r'''
    (?:
        (?:/[A-Z]|(?<![\w.-])[A-Z]:|//[\w.-]+/[A-Z]\$)/Users/
        | (?:(?<![\w.-])[A-Z]:|\\{2,}[\w.-]+\\+[A-Z]\$)\\+Users\\+
    )
'''
# Windows va primero para contar una sola vez el segmento /Users de una unidad.
PROFILE_HOME = re.compile(rf'''
    (?P<quote>['"]){WINDOWS_LOCATION}
        (?P<quoted_user>[\w][\w.-]*(?:\ +[\w.-]+)*)(?=[/\\]|(?P=quote))
    | {WINDOWS_LOCATION}(?P<windows_user>[\w][\w.-]*)
    | (?:file://(?:localhost)?|(?<![\w./-])-[A-Z]+|/System/Volumes/Data|(?<![\w./\\~$-]))
        /Users/(?P<mac_user>[\w][\w.-]*)
''', re.IGNORECASE | re.VERBOSE)
WSL_USER = re.compile(r'\bwsl\b(?:(?!\s--(?:\s|$)).)*?\s(?:-u|--user)\s+([a-z_][a-z0-9_-]*)(?![a-z0-9_<>-])')


def tailnet_ip(text, prefix):
    # Un CIDR describe una red; /32 es la ruta de un host concreto.
    if prefix is not None and prefix != '32':
        return False
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return ip in TAILNET and ip not in SYNTHETIC_NET


def findings(text):
    """Devuelve (línea, tipo) por cada dato interno de text."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if any(m[1].lower() not in SYNTHETIC_TAILNETS for m in FQDN.finditer(line)):
            found.append((number, 'FQDN de tailnet'))
        if any(tailnet_ip(m[1], m[2]) for m in IPV4.finditer(line)):
            found.append((number, 'IP de tailnet'))
        if any(m[1] not in SYNTHETIC_USERS for m in HOME.finditer(line)):
            found.append((number, 'home de usuario'))
        profiles = list(PROFILE_HOME.finditer(line.replace(r'\/', '/')))
        if any(m['mac_user'] and m['mac_user'].rstrip('.').lower() not in MAC_ALLOWED_USERS
               for m in profiles):
            found.append((number, 'home de macOS'))
        if any(user.rstrip('.').lower() not in WINDOWS_ALLOWED_USERS
               for m in profiles if (user := m['quoted_user'] or m['windows_user'])):
            found.append((number, 'home de Windows'))
        if any(m[1] not in SYNTHETIC_USERS for m in WSL_USER.finditer(line)):
            found.append((number, 'cuenta de WSL'))
    return found


def main():
    names = subprocess.run(['git', 'ls-files', '-z', '--', *SCOPE], check=True,
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
        print(str(total) + ' dato(s) interno(s) en ' + ' y '.join(SCOPE) + '; usa los marcadores de tests/datos_internos.py.',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
