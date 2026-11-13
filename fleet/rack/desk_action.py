"""Fixed Windows→WSL action dispatcher and bounded pipe exchange. Python 3.8+."""
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import signal
import stat
import subprocess
import sys
import time

SCHEMA = 'rack-desk-action@1'
HOME = Path('/home/felix')
CONTROLLERS = {
    'remote-run': '/home/felix/.local/lib/remote-run/run-control.py',
    'jale': '/home/felix/.local/lib/jale/pueue-control.py',
}
SWITCH = HOME / '.local/state/pao/rack/actions.disabled'
TIMEOUT = 10
REFS = {'remote-run': re.compile(r'[0-9]{8}-[0-9]{6}-[0-9a-f]{6}'),
        'jale': re.compile(r'job-[A-Za-z0-9]{8}')}
ACTION = re.compile(r'[A-Za-z0-9-]{8,64}')


def unique(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError()
        data[key] = value
    return data


def decode(raw):
    if len(raw) > 16384:
        raise ValueError()
    return json.loads(raw, object_pairs_hook=unique)


def exchange(argv, request, runner, env, timeout):
    """No shell/TTY, bounded stdin/stdout and time; kill only our session and reap."""
    deadline = time.monotonic() + timeout
    process = runner(argv,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env,
        cwd='/', start_new_session=True)
    output = bytearray()
    try:
        # Drain stdout while running; never spool an unbounded response to disk.
        # Nonblocking stdin also keeps a controller that never reads within the deadline.
        raw = memoryview(json.dumps(request).encode())
        with selectors.DefaultSelector() as selector:
            for stream, event in ((process.stdin, selectors.EVENT_WRITE),
                                  (process.stdout, selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, event)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(process.args, timeout)
                for key, event in selector.select(remaining):
                    if key.fileobj is process.stdin:
                        try:
                            raw = raw[os.write(process.stdin.fileno(), raw):]
                        except BrokenPipeError:
                            raw = raw[:0]
                        if not raw:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        chunk = os.read(process.stdout.fileno(), min(4096, 16385 - len(output)))
                        output.extend(chunk)
                        if len(output) > 16384:
                            raise ValueError('Respuesta demasiado grande')
                        if not chunk:
                            selector.unregister(process.stdout)
            process.wait(timeout=max(0, deadline - time.monotonic()))
    except BaseException:
        # Only the session we created, including descendants holding stdout open.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    finally:
        process.stdin.close()
        process.stdout.close()
    return process.returncode, decode(output)


def parse(raw):
    data = decode(raw)
    if (type(data) is not dict or set(data) != {'schema', 'launcher', 'request'}
            or data['schema'] != SCHEMA or type(data['launcher']) is not str):
        raise ValueError()
    launcher, request = data['launcher'], data['request']
    if launcher not in CONTROLLERS:
        raise LookupError()
    if type(request) is not dict or request.get('schema') != 'rack-cancel@1':
        raise ValueError()
    op = request.get('op')
    if type(op) is not str or op not in ('capabilities', 'cancel', 'status'):
        raise ValueError()
    keys = {'schema', 'op', 'ref'}
    if op != 'capabilities':
        keys.add('action_id')
        if type(request.get('action_id')) is not str or not ACTION.fullmatch(request['action_id']):
            raise ValueError()
    if op == 'cancel':
        keys.add('expected')
        expected = request.get('expected')
        if (type(expected) is not dict or set(expected) != {'task_id', 'state'}
                or type(expected['task_id']) is not int or expected['task_id'] < 0
                or type(expected['state']) is not str or expected['state'] not in ('Queued', 'Running')):
            raise ValueError()
    if (set(request) != keys or type(request.get('ref')) is not str
            or not REFS[launcher].fullmatch(request['ref'])):
        raise ValueError()
    return launcher, request


def failure(request, status, code, message):
    # Only echo identities that passed parse; never echo untrusted paths or values.
    return dict(schema='rack-cancel-result@1', ref=request.get('ref'),
                action_id=request.get('action_id'), status=status, result=None,
                error=dict(code=code, message=message))


def switched_off():
    """Only a definite absence permits a new effect, including broken symlinks."""
    try:
        SWITCH.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def environment():
    account = pwd.getpwuid(os.getuid())
    env = dict(HOME=str(HOME), PATH='/usr/bin:/bin', TMPDIR='/tmp',
               USER=account.pw_name, LOGNAME=account.pw_name)
    runtime = '/run/user/' + str(account.pw_uid)
    try:
        info = os.stat(runtime)
        if stat.S_ISDIR(info.st_mode) and info.st_uid == account.pw_uid:
            env['XDG_RUNTIME_DIR'] = runtime
    except OSError:
        pass
    return env


def handle(raw, runner=subprocess.Popen):
    try:
        launcher, request = parse(raw)
    except LookupError:
        return failure({}, 'rejected', 'unsupported', 'Lanzador desconocido.'), 2
    except (ValueError, TypeError, RecursionError):
        return failure({}, 'rejected', 'invalid_request', 'Envoltura inválida.'), 2
    # Independent of the panel switch. Queries remain available for accepted actions.
    if request['op'] in ('cancel', 'capabilities') and switched_off():
        if request['op'] == 'capabilities':
            return dict(schema='rack-cancel-result@1', ref=request['ref'],
                        capabilities=dict(queued=False, running=False, reason='Acciones apagadas en el desk.')), 0
        return failure(request, 'rejected', 'unsupported', 'Acciones apagadas en el desk.'), 0
    controller = Path(CONTROLLERS[launcher])
    if not controller.is_file():
        if request['op'] == 'capabilities':
            return dict(schema='rack-cancel-result@1', ref=request['ref'],
                        capabilities=dict(queued=False, running=False, reason='Controlador ausente.')), 0
        status = 'unknown' if request['op'] == 'status' else 'rejected'
        return failure(request, status, 'unsupported', 'Controlador ausente.'), 0
    try:
        code, response = exchange(['/usr/bin/python3', '-I', str(controller)], request, runner,
            environment(), TIMEOUT)
    except subprocess.TimeoutExpired:
        return failure(request, 'unknown', 'timeout', 'Plazo del controlador vencido.'), 4
    except ValueError:
        return failure(request, 'unknown', 'invalid_response', 'Respuesta inválida o excesiva.'), 5
    except Exception:
        return failure(request, 'unknown', 'controller_failed', 'Falló el controlador.'), 6
    if code not in (0, 2):
        return failure(request, 'unknown', 'controller_failed', 'Falló el controlador.'), 6
    if (type(response) is not dict or response.get('schema') != 'rack-cancel-result@1'
            or response.get('ref') != request['ref']
            or request['op'] != 'capabilities' and response.get('action_id') != request['action_id']):
        return failure(request, 'unknown', 'invalid_response', 'Respuesta inválida o excesiva.'), 5
    return response, code


def read_request(stream):
    """A forced-command client cannot hold stdin open forever or send unlimited data."""
    raw = bytearray()
    deadline = time.monotonic() + 5
    os.set_blocking(stream.fileno(), False)
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError()
            if not selector.select(remaining):
                continue
            chunk = os.read(stream.fileno(), min(4096, 16385 - len(raw)))
            raw.extend(chunk)
            if len(raw) > 16384:
                raise ValueError()
            if not chunk:
                return raw


def main():
    try:
        if len(sys.argv) != 1:
            raise ValueError()
        response, code = handle(read_request(sys.stdin.buffer))
    except (ValueError, OSError):
        response, code = failure({}, 'rejected', 'invalid_request', 'Envoltura inválida.'), 2
    except Exception:
        response, code = failure({}, 'unknown', 'controller_failed', 'Falló el despachador.'), 6
    print(json.dumps(response, ensure_ascii=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
