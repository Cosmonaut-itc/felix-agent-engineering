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
import json
import re
import subprocess
import sys

SCOPE = ('fleet/', 'scripts/')
TAILNET = ipaddress.ip_network('100.64.0.0/10')
SYNTHETIC_NET = ipaddress.ip_network('100.100.0.0/24')
SYNTHETIC_TAILNETS = {'example'}
SYNTHETIC_USERS = {'u', 'root'}

FQDN = re.compile(r'([A-Za-z0-9-]+)\.ts\.net(?![A-Za-z0-9-])', re.IGNORECASE)
IPV4 = re.compile(r'(?<![0-9.])(\d{1,3}(?:\.\d{1,3}){3})(?:/(\d{1,2}))?(?![0-9]|\.[0-9])')
HOME = re.compile(r'(?<![A-Za-z0-9_.~$-])/home/([a-z_][a-z0-9_-]*)(?![a-z0-9_-])')
PROFILE_HOME = re.compile(r'''
    (?:
        (?P<windows>
            (?P<win_prefix>
                (?:(?<![^\s"'=(,])|(?<=-I)) (?a:[A-Z]):
                | (?<![^\s"'=(,]) (?:
                    [/\\]{2,}[\w.-]+[/\\]+[\w.$-]+
                    | \\?/(?:mnt\\?/)?(?a:[A-Z])(?=\\?/)
                )
            )
            | (?a:[A-Z]):
        )
        | (?P<mac_prefix>
            (?<![^\s"'=:(,])
            (?: file:\\?/\\?/(?:localhost)?
                | \\?/Volumes\\?/[^/\\\s"']+
                | \\?/System\\?/Volumes\\?/Data
            )?
            (?=\\?/Users)
        )
        # Un esquema URI completo no acaba en una unidad.
        | (?<![^\s"'=(,]) (?a:[A-Z][A-Z0-9+.-]+): (?=//)
        | (?<![/\\])
    )
    [/\\]+Users(?=[/\\]+(?P<user>[\w][\w.-]*))
''', re.IGNORECASE | re.VERBOSE)
JSON_STRING = r'"(?:[^"\\]|\\.)*"'
JSON_LITERAL = re.compile(JSON_STRING)
QUOTED_PATH = re.compile(r"'[^']*'|" + JSON_STRING)
QUOTED_USER = re.compile(r'[\w][\w.-]*(?: +[\w.-]+)*(?=[/\\]|$)')
PROFILE_ESCAPES = {'%5c': '\\', '%2f': '/', '%20': ' '}
PROFILE_ESCAPE = re.compile('|'.join(PROFILE_ESCAPES), re.IGNORECASE)
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


def profile_variants(line):
    yield line
    decoded = PROFILE_ESCAPE.sub(lambda match: PROFILE_ESCAPES[match[0].lower()], line)
    if decoded != line:
        yield decoded
    for literal in JSON_LITERAL.finditer(line):
        try:
            yield json.loads(literal[0])
        except json.JSONDecodeError:
            continue


def clean_profile_end(line, end, closing_quote):
    if end == len(line) or end == closing_quote:
        return True
    following = line[end]
    if following == '\\':
        return not line.startswith(('\\ ', '\\u', '\\"'), end)
    # El primer espacio sin comillas separa la ruta de la prosa.
    return following == '/' or following.isspace() and closing_quote is None


def profiles_in_variant(line):
    quotes = iter(QUOTED_PATH.finditer(line))
    quote = next(quotes, None)
    for profile in PROFILE_HOME.finditer(line):
        start = profile.start('user')
        while quote is not None and quote.end() <= start:
            quote = next(quotes, None)
        closing_quote = quote.end() - 1 if quote and quote.start() < start else None

        user = profile['user'].lower()
        if user.rstrip('.') in SYNTHETIC_USERS:
            name = (QUOTED_USER.match(line, start, closing_quote)
                    if closing_quote is not None else None)
            if name is None or name[0].rstrip('.').lower() in SYNTHETIC_USERS:
                continue
        if clean_profile_end(line, profile.end('user'), closing_quote):
            if profile['win_prefix'] and user == 'public':
                continue
            if profile['mac_prefix'] is not None and user == 'shared':
                continue
        yield 'home de Windows' if profile['windows'] else 'home de macOS'


def profile_kinds(line):
    kinds = {kind for variant in profile_variants(line) for kind in profiles_in_variant(variant)}
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
