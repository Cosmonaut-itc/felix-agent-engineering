#!/usr/bin/python3 -I
"""Restricted Pueue 4.0.4 TOML editor. Never log the editing document."""
import json
import os
from pathlib import Path
import re
import stat
import sys


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


def document(raw, ident, priority):
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
            quote = text[i:i+1]
            if quote not in ('"', "'"):
                raise ValueError('edit_unsafe')
            triple = text[i:i+3] == quote*3
            delimiter = quote*(3 if triple else 1)
            i += len(delimiter)
            while True:
                if i >= len(text):
                    raise ValueError('edit_unsafe')
                if text.startswith(delimiter, i):
                    count = len(text[i:])-len(text[i:].lstrip(quote))
                    if triple and count > 5:
                        raise ValueError('edit_unsafe')
                    i += count if triple else 1
                    break
                if quote == '"' and text[i] == '\\':
                    escape = text[i+1:i+2]
                    if escape in ('u','U'):
                        length = 4 if escape=='u' else 8
                        if not re.fullmatch('[0-9A-Fa-f]{'+str(length)+'}',text[i+2:i+2+length]):
                            raise ValueError('edit_unsafe')
                        i += length+2
                    elif escape in ('b','t','n','f','r',chr(34),chr(92)):
                        i += 2
                    elif triple and escape in ('\n','\r'):
                        i += 2
                    else:
                        raise ValueError('edit_unsafe')
                else:
                    if not triple and text[i] in '\r\n':
                        raise ValueError('edit_unsafe')
                    i += 1
        end = re.match(r'[ \t]*(?:\r?\n|$)', text[i:])
        if not end:
            raise ValueError('edit_unsafe')
        i += end.end()
    if seen not in ({'id','command','path','priority'}, {'id','command','path','priority','label'}) or values['id'] != ident:
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


def private_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('edit_unsafe')


def main():
    plan_path = Path(sys.argv[2] if sys.argv[1] == '--client' else sys.argv[1]).resolve()
    private_file(plan_path)
    plan = json.loads(plan_path.read_text())
    root = plan_path.parent
    if root.stat().st_mode & 0o077 or (root/'revoked').exists():
        raise ValueError('edit_unsafe')
    if sys.argv[1] == '--client':
        # No RPC before the controller commits our PID + birth and opens stdin.
        if sys.stdin.buffer.read(1) != b'1':
            return
        os.execve(plan['binary'], [plan['binary'], '--config', str(root/'config.json'),
                                  'edit', str(plan['id'])], dict(os.environ))
    if birth(plan['client']['pid']) != plan['client']['birth']:
        raise ValueError('edit_unsafe')
    target = Path(sys.argv[2]).resolve()
    if target.name != 'tasks.toml' or root not in target.parents:
        raise ValueError('edit_unsafe')
    # Pueue's own temporary directory is inside our private TMPDIR.
    pid_path = root/'editor.json'
    fd = os.open(str(pid_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(dict(pid=os.getpid(), birth=birth(os.getpid())), stream)
        stream.flush(); os.fsync(stream.fileno())
    if (root/'revoked').exists():
        raise ValueError('edit_unsafe')
    raw = target.read_bytes()
    changed = document(raw, plan['id'], plan['priority'])
    with target.open('wb') as stream:
        stream.write(changed); stream.flush(); os.fsync(stream.fileno())


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # A parse exception must never echo commands, envs, paths or TOML.
        sys.exit(2)
