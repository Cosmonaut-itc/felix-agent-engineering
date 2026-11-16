#!/usr/bin/python3 -I
"""Editor fijo de `pueue edit` (modo TOML) que sólo cambia `priority`.

Pueue 4.0.4 invoca `sh -c "$EDITOR <ruta de tasks.toml>"`. Este helper recibe
la ruta del plan como primer argumento (fija, la pone control.py) y la del
TOML como último. No acepta rutas ni guiones desde JSON, no escribe ni
registra comandos, rutas, etiquetas ni entornos: sólo sale con un código.

El TOML lo emite el serializador de Pueue para una sola `EditableTask`:
una tabla `[ID]` con `id`, `command`, `path`, `label` opcional y `priority`.
Se reconoce ese subconjunto exacto; cualquier otra forma se rechaza, y una
línea `priority = N` dentro de una cadena multilínea nunca se confunde con la
clave, porque el escáner recorre las cadenas antes de buscar claves.
Python 3.9 y biblioteca estándar.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

KEYS = ('id', 'command', 'path', 'label', 'priority')
REQUIRED = {'id', 'command', 'path', 'priority'}
ESCAPES = {'b': '\b', 't': '\t', 'n': '\n', 'f': '\f', 'r': '\r', '"': '"', '\\': '\\'}
EXIT_FORMAT, EXIT_PLAN, EXIT_IDENTITY, EXIT_IO = 3, 4, 5, 6


class Unrecognized(ValueError):
    pass


def fingerprint(command, path, label):
    """Opaque digest of what an edit may never change; never the values themselves."""
    body = json.dumps([command, path, label], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def _basic(text, i, end_quote):
    """Decode a basic string body starting after its opening quote(s)."""
    out = []
    while True:
        if i >= len(text):
            raise Unrecognized('cadena sin cerrar')
        c = text[i]
        if c == '\\':
            if i + 1 >= len(text):
                raise Unrecognized('escape incompleto')
            e = text[i + 1]
            if e in ESCAPES:
                out.append(ESCAPES[e]); i += 2
            elif e in 'uU':
                width = 4 if e == 'u' else 8
                digits = text[i + 2:i + 2 + width]
                if not re.fullmatch('[0-9A-Fa-f]{%d}' % width, digits):
                    raise Unrecognized('escape unicode inválido')
                code = int(digits, 16)
                if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                    raise Unrecognized('escape unicode inválido')
                out.append(chr(code)); i += 2 + width
            else:
                # Includes the multiline line-ending backslash: not emitted by Pueue.
                raise Unrecognized('escape no reconocido')
            continue
        if end_quote == '"':
            if c == '"':
                return ''.join(out), i + 1
            if c == '\n' or c == '\r':
                raise Unrecognized('salto en cadena simple')
        elif text.startswith('"""', i):
            run = len(text) - i - len(text[i:].lstrip('"'))
            if run > 5:
                raise Unrecognized('comillas finales ambiguas')
            out.append('"' * (run - 3))
            return ''.join(out), i + run
        out.append(c); i += 1


def _literal(text, i, multiline):
    if multiline:
        end = text.find("'''", i)
        if end < 0:
            raise Unrecognized('cadena literal sin cerrar')
        run = len(text) - end - len(text[end:].lstrip("'"))
        if run > 5:
            raise Unrecognized('comillas finales ambiguas')
        return text[i:end] + "'" * (run - 3), end + run
    end = text.find("'", i)
    if end < 0 or '\n' in text[i:end] or '\r' in text[i:end]:
        raise Unrecognized('cadena literal inválida')
    return text[i:end], end + 1


def _value(text, i):
    """Return (kind, decoded, start, end) of one TOML value at text[i]."""
    if text.startswith('"""', i):
        j = i + 3
        if text.startswith('\n', j):
            j += 1  # the newline right after the opening delimiter is trimmed
        value, end = _basic(text, j, '"""')
        return 'string', value, i, end
    if text.startswith("'''", i):
        j = i + 3
        if text.startswith('\n', j):
            j += 1
        value, end = _literal(text, j, True)
        return 'string', value, i, end
    if text.startswith('"', i):
        value, end = _basic(text, i + 1, '"')
        return 'string', value, i, end
    if text.startswith("'", i):
        value, end = _literal(text, i + 1, False)
        return 'string', value, i, end
    match = re.compile(r'-?(?:0|[1-9][0-9]*)').match(text, i)
    if match:
        return 'integer', int(match.group()), i, match.end()
    raise Unrecognized('valor no reconocido')


def parse(text):
    """Recognize exactly one Pueue EditableTask table; return fields and the priority span."""
    if '\r' in text or '\t' in text:
        # Pueue escapes tabs/CR inside strings; a raw one is a format we did not prove.
        raise Unrecognized('carácter de control fuera de cadena')
    i = 0
    header = re.compile(r'\[(0|[1-9][0-9]*)\]\n').match(text, i)
    if not header:
        raise Unrecognized('cabecera de tabla no reconocida')
    table = int(header.group(1))
    i = header.end()
    fields, spans, order = {}, {}, []
    while i < len(text):
        if text[i] == '\n':
            i += 1
            continue
        key = re.compile(r'([a-z]+) = ').match(text, i)
        if not key or key.group(1) not in KEYS:
            raise Unrecognized('clave no reconocida')
        kind, value, start, end = _value(text, key.end())
        if end < len(text) and text[end] != '\n':
            raise Unrecognized('texto tras el valor')
        name = key.group(1)
        if (name in ('id', 'priority')) != (kind == 'integer'):
            raise Unrecognized('tipo inesperado')
        fields[name], spans[name] = value, (start, end)
        order.append(name)
        i = end
    if not REQUIRED <= set(fields) or order != [k for k in KEYS if k in fields]:
        raise Unrecognized('tabla incompleta, desordenada o con clave repetida')
    if fields['id'] != table:
        raise Unrecognized('id de tabla incoherente')
    if not -(2**31) <= fields['priority'] < 2**31:
        raise Unrecognized('prioridad fuera de i32')
    return dict(id=table, command=fields['command'], path=fields['path'], label=fields.get('label'),
                priority=fields['priority'], span=spans['priority'])


def rewrite(text, plan):
    """Pure: validate against the plan before the only change, then splice the digits."""
    task = parse(text)
    if task['id'] != plan['id'] or task['priority'] != plan['before']:
        raise LookupError('plan')
    if fingerprint(task['command'], task['path'], task['label']) != plan['fingerprint']:
        raise PermissionError('identidad')
    start, end = task['span']
    result = text[:start] + str(plan['target']) + text[end:]
    check = parse(result)
    if (check['priority'] != plan['target'] or
            [check[k] for k in ('id', 'command', 'path', 'label')] != [task[k] for k in ('id', 'command', 'path', 'label')]):
        raise Unrecognized('reescritura no verificable')
    return result


def private(path, mode):
    info = os.lstat(path)
    return (stat.S_ISDIR(info.st_mode) if mode == 0o700 else stat.S_ISREG(info.st_mode)) and \
        info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == mode


def load_plan(path):
    path = Path(path)
    if not path.is_absolute() or not private(path.parent, 0o700) or not private(path, 0o600):
        raise LookupError('plan')
    plan = json.loads(path.read_text(encoding='utf-8'))
    if (type(plan) is not dict or set(plan) != {'version', 'id', 'before', 'target', 'fingerprint'}
            or plan['version'] != 1 or any(type(plan[k]) is not int for k in ('id', 'before', 'target'))
            or not 0 <= plan['id'] < 2**64
            or not all(-(2**31) <= plan[k] < 2**31 for k in ('before', 'target'))
            or type(plan['fingerprint']) is not str or not re.fullmatch('[0-9a-f]{64}', plan['fingerprint'])):
        raise LookupError('plan')
    return plan


def main(argv):
    if len(argv) != 3:
        return EXIT_PLAN
    try:
        plan = load_plan(argv[1])
        target = Path(argv[2])
        # Pueue creates tasks.toml under TMPDIR; control.py sets TMPDIR to the
        # plan's private directory. Anything else is not our edit.
        root = Path(argv[1]).parent.resolve()
        if target.name != 'tasks.toml' or root not in target.resolve().parents or target.is_symlink():
            return EXIT_PLAN
        raw = target.read_bytes()
        result = rewrite(raw.decode('utf-8'), plan)
        temporary = target.with_name('.tasks.toml.tmp')
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(result.encode('utf-8'))
        os.replace(temporary, target)
        return 0
    except LookupError:
        return EXIT_PLAN
    except PermissionError:
        return EXIT_IDENTITY
    except (Unrecognized, UnicodeDecodeError, ValueError):
        return EXIT_FORMAT
    except OSError:
        return EXIT_IO


if __name__ == '__main__':
    sys.exit(main(sys.argv))
