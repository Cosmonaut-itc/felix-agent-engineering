#!/usr/bin/python3 -I
"""Restricted Pueue 4.0.4 TOML editor. Never log the editing document."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


def birth(pid):
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='],
                            capture_output=True, text=True, timeout=.3)
    return result.stdout.strip() if result.returncode == 0 else None


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
        key = re.match(r'[ \t]*(id|original_command|path|label|priority)[ \t]*=[ \t]*', text[i:])
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
                    i += len(delimiter)
                    # TOML allows four/five quote endings. Reject instead of guessing.
                    if text[i:i+1] == quote:
                        raise ValueError('edit_unsafe')
                    break
                if quote == '"' and text[i] == '\\':
                    i += 2
                else:
                    if not triple and text[i] in '\r\n':
                        raise ValueError('edit_unsafe')
                    i += 1
        end = re.match(r'[ \t]*(?:\r?\n|$)', text[i:])
        if not end:
            raise ValueError('edit_unsafe')
        i += end.end()
    if seen not in ({'id','original_command','path','priority'}, {'id','original_command','path','priority','label'}) or values['id'] != ident:
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
    # Exercise our grammar against the complete anticipated editable fields.
    raw = ('[' + str(task['id']) + ']\nid = ' + str(task['id']) + '\n' +
           ''.join(k + ' = ' + json.dumps(task[k], ensure_ascii=False) + '\n'
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
