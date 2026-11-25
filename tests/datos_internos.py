#!/usr/bin/env python3
"""Rechaza datos internos de acceso en fleet/ y scripts/.

El repositorio es público. Un FQDN de tailnet, una IP de 100.64.0.0/10 o un
home de Linux, macOS o Windows van con marcadores: <host>.<tailnet>.ts.net,
<ip-tailnet-...>, <home>, /home/<usuario>, /c/Users/<usuario> y
`wsl -u <usuario>`. Las pruebas usan example.ts.net, 100.100.0.0/24,
la cuenta u y los demás nombres de SYNTHETIC_USERS.

Si una prueba requiere una ruta de perfil, añade el comentario
«datos-internos: sintético» en esa línea. El marcador sólo exime perfiles.

La salida nombra archivo, línea y tipo, nunca el valor: el log de CI también
es público.
"""
import ipaddress
import re
import subprocess
import sys
from urllib.parse import unquote

SCOPE = ('fleet/', 'scripts/')
TAILNET = ipaddress.ip_network('100.64.0.0/10')
SYNTHETIC_NET = ipaddress.ip_network('100.100.0.0/24')
SYNTHETIC_TAILNETS = {'example'}
SYNTHETIC_USERS = {'u', 'root'}

FQDN = re.compile(r'([A-Za-z0-9-]+)\.ts\.net(?![A-Za-z0-9-])', re.IGNORECASE)
IPV4 = re.compile(r'(?<![0-9.])(\d{1,3}(?:\.\d{1,3}){3})(?:/(\d{1,2}))?(?![0-9]|\.[0-9])')
HOME = re.compile(r'(?<![A-Za-z0-9_.~$-])/home/([a-z_][a-z0-9_-]*)(?![a-z0-9_-])')
PROFILE_SEGMENT = re.compile(r'(?<![/\\])[/\\]+Users(?=[/\\]+\w)', re.IGNORECASE)
WINDOWS_PREFIX = re.compile(r'''
    (?:
        # Un esquema URI completo no acaba en una unidad.
        (?P<scheme>(?<![\w+.-])(?a:[A-Z][A-Z0-9+.-]+):(?=//))
        | (?a:[A-Z]):
        | (?<![\w/\\%]) /+(?:mnt/+)?(?a:[A-Z])
        | (?<![\w/\\:]) [/\\]{2,}[\w.?!-]+[/\\]+[\w.$ -]+
    )
    (?=[/\\]+Users)
''', re.IGNORECASE | re.VERBOSE)
JSON_ESCAPE = re.compile(
    r'\\(?:u[dD][89aAbB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}'
    r'|u[0-9a-fA-F]{4}|[/\\])')
SYNTHETIC_PROFILE_MARKER = 'datos-internos: sintético'
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


def decode_json_escape(match):
    escape = match[0]
    if len(escape) == 2:
        return escape[1]
    try:
        # Los pares sustitutos forman un solo carácter Unicode.
        return bytes.fromhex(escape[2:6] + escape[8:]).decode('utf-16-be')
    except UnicodeDecodeError:
        return escape


def profile_forms(line):
    yield line
    for _ in range(4):
        previous = line
        # Un byte inválido no impide decodificar los separadores de la misma línea.
        line = unquote(line, errors='replace')
        if line != previous:
            yield line
        decoded = JSON_ESCAPE.sub(decode_json_escape, line)
        if decoded != line:
            yield decoded
        line = decoded
        if line == previous:
            break


def profile_kinds(line):
    if SYNTHETIC_PROFILE_MARKER in line:
        return []
    kinds = set()
    for form in profile_forms(line):
        profiles = list(PROFILE_SEGMENT.finditer(form))
        if profiles:
            windows = {prefix.end() for prefix in WINDOWS_PREFIX.finditer(form) if not prefix['scheme']}
            for profile in profiles:
                is_windows = profile.start() in windows or form[profile.end()] == '\\'
                kinds.add('home de Windows' if is_windows else 'home de macOS')
    return [kind for kind in ('home de macOS', 'home de Windows') if kind in kinds]


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
        found.extend((number, kind) for kind in profile_kinds(line))
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
