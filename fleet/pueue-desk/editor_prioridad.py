#!/usr/bin/python3 -I
"""Restricted Pueue 4.0.4 TOML editor. Never log the editing document."""
import time
_ENTRY = time.clock_gettime(time.CLOCK_MONOTONIC)
import sys
import json
import os
import stat
# The gated client never parses TOML, hashes fields or invokes a status child.
# Avoid editor-only dependencies for that extra interpreter on every edit.
if __name__ != '__main__' or '--client' not in sys.argv:
    from pathlib import Path
    import re
    import hashlib
    import subprocess
_IMPORTED = time.clock_gettime(time.CLOCK_MONOTONIC)


def birth(pid):
    """Kernel start identity; zombies cannot submit a late EditedTasks."""
    if type(pid) is not int or pid <= 0:
        raise ValueError('edit_unsafe')
    if sys.platform == 'darwin':
        import ctypes
        class BSDInfo(ctypes.Structure):
            _fields_ = [(n,ctypes.c_uint32) for n in ('flags','status','xstatus','pid','ppid','uid','gid','ruid','rgid','svuid','svgid','rfu')]
            _fields_ += [('comm',ctypes.c_char*16),('name',ctypes.c_char*32)]
            _fields_ += [(n,ctypes.c_uint32) for n in ('nfiles','pgid','pjobc','e_tdev','e_tpgid','nice')]
            _fields_ += [('sec',ctypes.c_uint64),('usec',ctypes.c_uint64)]
        library=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
        info=BSDInfo()
        count=library.proc_pidinfo(pid,3,0,ctypes.byref(info),ctypes.sizeof(info))
        if count==ctypes.sizeof(info):
            if info.uid != os.getuid():
                raise ValueError('edit_unsafe')
            return None if info.status==5 else 'darwin:'+str(info.sec)+':'+str(info.usec)
        try:
            os.kill(pid,0)
        except ProcessLookupError:
            return None
        raise OSError('No se acreditó la identidad del proceso propio')
    if sys.platform == 'linux':
        try:
            record=(Path('/proc')/str(pid)/'stat').read_text()
        except FileNotFoundError:
            return None
        fields=record[record.rfind(')')+2:].split()
        if fields[0]=='Z':
            return None
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return 'linux:'+boot+':'+fields[19]
    raise OSError('Identidad de proceso no soportada')


def integer(n):
    if type(n) is not int or not -(2**31) <= n < 2**31:
        raise ValueError('edit_unsafe')
    return n


ESCAPES = {'b': '\b', 't': '\t', 'n': '\n', 'f': '\f', 'r': '\r', '"': '"', '\\': '\\'}


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
            elif end_quote == chr(34)*3 and e in ('\n','\r'):
                i += 1
                if text.startswith('\r\n',i):i += 2
                elif text.startswith('\n',i):i += 1
                else:raise Unrecognized('continuación inválida')
                while i<len(text) and text[i] in ' \t\r\n':i += 1
            else:
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
        if c=='\r' and text[i:i+2]=='\r\n':
            out.append('\n');i += 2
        else:
            out.append(c);i += 1


def _literal(text, i, multiline):
    if multiline:
        end = text.find("'''", i)
        if end < 0:
            raise Unrecognized('cadena literal sin cerrar')
        run = len(text) - end - len(text[end:].lstrip("'"))
        if run > 5:
            raise Unrecognized('comillas finales ambiguas')
        return text[i:end].replace('\r\n','\n') + "'" * (run - 3), end + run
    end = text.find("'", i)
    if end < 0 or '\n' in text[i:end] or '\r' in text[i:end]:
        raise Unrecognized('cadena literal inválida')
    return text[i:end], end + 1


def _value(text, i):
    """Return (kind, decoded, start, end) of one TOML value at text[i]."""
    if text.startswith('"""', i):
        j = i + 3
        if text.startswith('\r\n',j):j += 2
        elif text.startswith('\n', j):
            j += 1  # the newline right after the opening delimiter is trimmed
        value, end = _basic(text, j, '"""')
        return 'string', value, i, end
    if text.startswith("'''", i):
        j = i + 3
        if text.startswith('\r\n',j):j += 2
        elif text.startswith('\n', j):
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



def document(raw, ident, priority, expected=None):
    """Scan only the serializer's scalar table grammar, not general TOML.

    Strings are consumed in their entirety before looking for the next key.
    This makes a line named priority inside a multiline command inert. Unknown
    tables, duplicates, arrays, inline maps and unfamiliar values fail closed.
    All bytes except the one integer's span survive unchanged.
    """
    integer(priority)
    text = raw.decode('utf-8')
    header = re.match(r'\A\s*\[(?:"' + str(ident) + r'"|' + str(ident) + r')\][ \t]*\r?\n', text)
    if not header:
        raise ValueError('edit_unsafe')
    i, seen, span, values = header.end(), set(), None, {}
    while i < len(text):
        empty = re.match(r'(?:[ \t]*\r?\n)+', text[i:])
        if empty:
            i += empty.end()
            continue
        key = re.match(r'[ \t]*(id|command|path|label|priority)[ \t]*=[ \t]*', text[i:])
        if not key or key[1] in seen:
            raise ValueError('edit_unsafe')
        name = key[1]; seen.add(name); i += key.end(); start = i
        if name in ('id', 'priority'):
            value = re.match(r'-?(?:0|[1-9][0-9]*)', text[i:])
            if not value:
                raise ValueError('edit_unsafe')
            i += value.end(); values[name] = int(value[0])
            if name == 'priority':
                integer(values[name]); span = (start, i)
        else:
            kind, value, _, i = _value(text, i)
            if kind != 'string':
                raise ValueError('edit_unsafe')
            values[name] = value
        end = re.match(r'[ \t]*(?:\r?\n|$)', text[i:])
        if not end:
            raise ValueError('edit_unsafe')
        i += end.end()
    if seen not in ({'id','command','path','priority'}, {'id','command','path','priority','label'}) or values['id'] != ident:
        raise ValueError('edit_unsafe')
    if expected is not None:
        if (values['priority'] != expected['expected_priority'] or
                fingerprint(values['command'], values['path'], values.get('label')) != expected['fingerprint']):
            raise ValueError('edit_unsafe')
    return (text[:span[0]] + str(priority) + text[span[1]:]).encode('utf-8')


def preflight(task):
    # EditedTasks expands aliases again. No alias-dependent task is editable.
    if task['command'] != task['original_command']:
        raise ValueError('edit_unsafe')
    for key in ('original_command', 'path'):
        if type(task[key]) is not str or not task[key] or '\x00' in task[key]:
            raise ValueError('edit_unsafe')
    if task['label'] is not None and type(task['label']) is not str:
        raise ValueError('edit_unsafe')
    for value in (task['original_command'],task['path'],task['label']):
        if value is not None and (chr(34)*3 in value or chr(39)*3 in value):
            raise ValueError('edit_unsafe')
    # Exercise our grammar against the complete anticipated editable fields.
    raw = ('[' + str(task['id']) + ']\nid = ' + str(task['id']) + '\n' +
           ''.join(('command' if k=='original_command' else k) + ' = ' + json.dumps(task[k], ensure_ascii=False) + '\n'
                   for k in ('original_command','path','label') if task[k] is not None) +
           'priority = ' + str(integer(task['priority'])) + '\n').encode()
    document(raw, task['id'], task['priority'])


def verify_locked(plan, snapshot):
    task=snapshot['tasks'].get(str(plan['id']))
    fields=('id','created_at','group','command','original_command','path','envs','dependencies','label')
    if (task is None or set(task['status'])!={'Locked'}
            or set(task['status']['Locked']['previous_status'])!={'Queued'}
            or snapshot['groups'][task['group']]['status']!='Paused'
            or task['priority']!=plan['expected_priority']):
        raise ValueError('edit_unsafe')
    identity=hashlib.sha256(json.dumps({k:task[k] for k in fields},sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    if identity!=plan['identity']:
        raise ValueError('edit_unsafe')


def private_file(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('edit_unsafe')


def host_clock():
    # Python 3.9 macOS monotonic() has a process-local epoch. CLOCK_MONOTONIC
    # is the shared kernel clock; persisted plans only use it within one boot.
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def remaining(plan):
    left = min(1.0, plan['deadline'] - host_clock())
    if left <= 0:
        raise TimeoutError('pause_budget')
    return left


def trace_mark(root, marks, name):
    """Only fixed phase names and shared kernel timestamps; retain partial traces."""
    marks[name] = host_clock()
    path = os.path.join(root, 'trace-client.json' if '--client' in sys.argv else 'trace-helper.json')
    write_trace(path, marks)


def write_trace(path, marks):
    # Non-authoritative diagnostics: atomic visibility without a durable fsync
    # in the observer that would delay the next timestamp.
    path = os.fspath(path)
    temporary = path[:-5] + '.pending'
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(marks, stream)
    os.replace(temporary, path)


def process_started():
    """Darwin creation instant, converted between two kernel uptime clocks."""
    if sys.platform != 'darwin':
        return None
    import ctypes
    class Usage(ctypes.Structure):
        _fields_ = [('uuid', ctypes.c_char * 16)] + [(n, ctypes.c_uint64) for n in
            ('user','system','idle','interrupt','pageins','wired','resident','physical','start','exit')]
    class Timebase(ctypes.Structure):
        _fields_ = [('numer', ctypes.c_uint32), ('denom', ctypes.c_uint32)]
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    kernel = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    kernel.mach_absolute_time.restype = ctypes.c_uint64
    usage, scale = Usage(), Timebase()
    if library.proc_pid_rusage(os.getpid(), 0, ctypes.byref(usage)) or kernel.mach_timebase_info(ctypes.byref(scale)):
        return None  # diagnostics never establish ownership
    now = kernel.mach_absolute_time()
    return host_clock() - (now - usage.start) * scale.numer / scale.denom / 1e9


def main():
    begun=host_clock()
    marks = dict(entry=_ENTRY, imports=_IMPORTED, main=begun)
    if sys.argv[1] == '--client' and sys.stdin.buffer.read(1) != b'1':
        return
    marks['gate_read'] = host_clock()
    plan_path = os.path.realpath(sys.argv[2] if sys.argv[1] == '--client' else sys.argv[1])
    private_file(plan_path)
    with open(plan_path) as stream:
        plan = json.load(stream)
    root = os.path.dirname(plan_path)
    if os.stat(root).st_mode & 0o077 or os.path.exists(os.path.join(root, 'revoked')):
        raise ValueError('edit_unsafe')
    trace_mark(root, marks, 'plan_validated')
    if sys.argv[1] == '--client':
        # No RPC before the controller commits our PID + birth and opens stdin.
        remaining(plan)
        trace_mark(root, marks, 'cli_exec')
        return os.execve(plan['binary'], [plan['binary'], '--config', plan['config'],
                                  'edit', str(plan['id'])], dict(os.environ))
    root = Path(root)
    trace_mark(root, marks, 'identity_begin')
    if birth(plan['client']['pid']) != plan['client']['birth']:
        raise ValueError('edit_unsafe')
    trace_mark(root, marks, 'client_identity')
    target = Path(sys.argv[2]).resolve()
    if target.name != 'tasks.toml' or root not in target.parents:
        raise ValueError('edit_unsafe')
    # Pueue's own temporary directory is inside our private TMPDIR.
    pid_path = root/'editor.json'
    fd = os.open(str(pid_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(dict(pid=os.getpid(), birth=birth(os.getpid())), stream)
        stream.flush(); os.fsync(stream.fileno())
    trace_mark(root, marks, 'self_identity')
    marks['created'] = process_started()
    if (root/'revoked').exists():
        raise ValueError('edit_unsafe')
    if birth(plan['daemon']['pid']) != plan['daemon']['birth']:
        raise ValueError('edit_unsafe')
    trace_mark(root, marks, 'daemon_identity')
    status_begun=host_clock()
    reply=subprocess.run([plan['binary'],'--config',plan['config'],'status','--json'],
                         check=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,
                         timeout=remaining(plan))
    verify_locked(plan,json.loads(reply.stdout))
    status_seconds=host_clock()-status_begun
    if birth(plan['daemon']['pid']) != plan['daemon']['birth']:
        raise ValueError('edit_unsafe')
    remaining(plan)
    write_begun=host_clock()
    raw = target.read_bytes()
    changed = document(raw, plan['id'], plan['priority'], expected=plan)
    if (root/'revoked').exists() or birth(plan['client']['pid'])!=plan['client']['birth']:
        raise ValueError('edit_unsafe')
    remaining(plan)
    with target.open('wb') as stream:
        stream.write(changed); stream.flush(); os.fsync(stream.fileno())
    # Diagnostic only: fixed names/numbers, never any editable content.
    (root/'metrics.json').write_text(json.dumps(dict(helper_start=begun-plan['launched'],
        helper_status=status_seconds,helper_write=host_clock()-write_begun)))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # A parse exception must never echo commands, envs, paths or TOML.
        sys.exit(2)
