#!/usr/bin/env python3
"""Dashboard del rack, sólo dentro del tailnet."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.request

BASE = Path(__file__).resolve().parent
TAIL4 = ipaddress.ip_network('100.64.0.0/10')
TAIL6 = ipaddress.ip_network('fd7a:115c:a1e0::/48')
TASK_STATES = ('Queued', 'Stashed', 'Locked', 'Running', 'Paused', 'Done')
TASK_TEXT = ('name', 'scope', 'project', 'ticket', 'repo', 'sha', 'result', 'branch', 'subject')
TASK_TIMES = ('enqueued', 'start', 'end')
LOOKUP_BUDGET = 8


def atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_cache(path, fields):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    def valid(entry):
        return isinstance(entry, dict) and all(
            k in fields and (v is None or (isinstance(v, fields[k]) and not isinstance(v, bool)
                                           and (not isinstance(v, (int, float)) or 0 <= v < 1e11)))
            for k, v in entry.items())
    return {k: v for k, v in data.items() if valid(v)} if isinstance(data, dict) else {}


def load_hosts(path):
    try:
        hosts = json.loads(path.read_text())
        if not isinstance(hosts, list) or not hosts:
            raise ValueError()
        ids = set()
        for host in hosts:
            if (not isinstance(host, dict) or set(host) != {'id', 'name', 'transport', 'argv'}
                    or not isinstance(host['id'], str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', host['id'])
                    or host['id'] in ids or not isinstance(host['name'], str) or not host['name']
                    or host['transport'] not in ('local', 'ssh') or not isinstance(host['argv'], list)
                    or not host['argv'] or any(not isinstance(a, str) or not a or '\0' in a for a in host['argv'])):
                raise ValueError()
            ids.add(host['id'])
        return hosts
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ValueError('hosts.json inválido: se requiere una lista de hosts únicos con id, name, transport y argv') from error


def normalize(name):
    return re.sub(r'(?:-(?:sol|opus|r\d+))+$', '', name or '')


class History:
    def __init__(self, directory):
        self.path = directory / 'history.jsonl'
        self.rows = []
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                try:
                    row = json.loads(line)
                    if (isinstance(row, dict) and all(k in row for k in ('host', 'id', 'group', 'name', 'result'))
                            and all(isinstance(row.get(k), (int, float)) for k in ('start', 'end'))):
                        self.rows.append(row)
                except ValueError:
                    continue

    def add(self, host, tasks, now):
        self.rows = [r for r in self.rows if r['end'] >= now - 30 * 86400]
        seen = {(r['host'], r['id'], r['start']) for r in self.rows}
        for task in tasks:
            key = host, task['id'], task.get('start')
            if (task['state'] == 'Done' and task.get('start') is not None and task.get('end') is not None
                    and task['end'] >= max(task['start'], now - 30 * 86400) and key not in seen):
                self.rows.append(dict(host=host, **{k: task.get(k) for k in ('id', 'start', 'end', 'group', 'name', 'result')}))
                seen.add(key)
        atomic(self.path, ''.join(json.dumps(r) + '\n' for r in self.rows))

    def estimate(self, host, task):
        matches = sorted((r for r in self.rows if r['host'] == host and r['group'] == task['group']
                          and normalize(r['name']) == normalize(task['name']) and r['result'] == 'Success'), key=lambda r: r['end'])[-20:]
        if len(matches) < 5:
            return None
        durations = sorted(r['end'] - r['start'] for r in matches)
        def percentile(p):
            position = (len(durations) - 1) * p
            lo, hi = math.floor(position), math.ceil(position)
            return durations[lo] + (durations[hi] - durations[lo]) * (position - lo)
        return dict(typical=statistics.median(durations), low=percentile(.1), high=percentile(.9), n=len(matches))


def validate_snapshot(data, host):
    if not isinstance(data, dict) or data.get('schema') != 'rack-host@1' or data.get('host') != host:
        raise ValueError('Esquema o host inválido')
    if not isinstance(data.get('taken'), (float, int)) or not math.isfinite(data['taken']):
        raise ValueError('Fecha inválida')
    if not isinstance(data.get('tasks'), list) or not isinstance(data.get('groups'), list):
        raise ValueError('Tareas o grupos inválidos')
    for row in data['tasks']:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), int) or not isinstance(row.get('group'), str)
                or not isinstance(row.get('priority'), int) or isinstance(row.get('priority'), bool)
                or row.get('state') not in TASK_STATES
                or any(k not in row for k in TASK_TEXT + TASK_TIMES)
                or any(not isinstance(row.get(k), (str, type(None))) for k in TASK_TEXT)
                or any(not isinstance(row.get(k), (int, float, type(None))) for k in TASK_TIMES)):
            raise ValueError('Tarea inválida')
    if any(not isinstance(data.get(k), (dict, type(None))) for k in ('cpu', 'mem', 'disk', 'guard')):
        raise ValueError('Capacidad inválida')
    if any(isinstance(e, dict) and e.get('stage') == 'pueue' for e in data.get('errors') or []):
        raise ValueError('Pueue no se pudo leer')
    json.dumps(data, allow_nan=False)
    for group in data['groups']:
        if not isinstance(group, dict) or not isinstance(group.get('slots'), int) or not 0 <= group['slots'] <= 1000 or not isinstance(group.get('name'), str):
            raise ValueError('Grupo inválido')
    def safe(value):
        if isinstance(value, dict):
            return {k: safe(v) for k, v in value.items() if k not in ('envs', 'command', 'original_command', 'path')}
        return [safe(v) for v in value] if isinstance(value, list) else value
    return safe(data)


def fetch_host(host):
    argv = host['argv']
    if host['transport'] == 'local':
        argv = [a.replace('{rack_dir}', str(BASE)) for a in argv]
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=25, check=True)
    return validate_snapshot(json.loads(result.stdout), host['id'])


class Rack:
    def __init__(self, hosts, state_dir, key_path=None, gh='/opt/homebrew/bin/gh'):
        self.hosts = hosts
        self.good = {}
        self.errors = {}
        self.history = History(state_dir)
        self.ticket_path = state_dir / 'tickets.json'
        self.tickets = read_cache(self.ticket_path, dict(title=str, project=str, updated=(int, float), failed=(int, float)))
        self.gh = gh
        self.commit_path = state_dir / 'commits.json'
        self.commits = read_cache(self.commit_path, dict(ticket=str, branch=str, subject=str, checked=(int, float)))
        self.key = None
        key_path = key_path or Path.home() / '.config/pao-rack/linear-key'
        try:
            if key_path.stat().st_mode & 0o777 != 0o600:
                raise ValueError()
            self.key = key_path.read_text().strip()
            if not self.key:
                raise ValueError()
        except (OSError, ValueError):
            logging.warning('Linear sin títulos: falta linear-key con modo 600')
        self.lock = threading.Lock()
        self.payload = dict(schema='rack@1', now=time.time(), hosts=[], tickets={})

    def refresh_tickets(self, tickets, now, deadline=math.inf):
        count = 0
        if not self.key:
            return
        for ticket in sorted(tickets):
            if count >= 10 or time.monotonic() >= deadline:
                break
            known = self.tickets.get(ticket, {})
            if now - (known.get('updated') or 0) < 6 * 3600 or now - (known.get('failed') or 0) < 1800:
                continue
            count += 1
            try:
                query = 'query($id: String!) { issue(id: $id) { title project { name } } }'
                request = urllib.request.Request('https://api.linear.app/graphql',
                    data=json.dumps(dict(query=query, variables=dict(id=ticket))).encode(),
                    headers={'Authorization': self.key, 'Content-Type': 'application/json'})
                with urllib.request.urlopen(request, timeout=5) as response:
                    issue = json.load(response)['data']['issue']
                self.tickets[ticket] = dict(title=issue['title'], project=(issue.get('project') or {}).get('name'), updated=now)
            except Exception:
                self.tickets[ticket] = dict(known, failed=now)
                logging.warning('No se pudo actualizar un ticket de Linear')
        if count:
            atomic(self.ticket_path, json.dumps(self.tickets))

    def resolve_commits(self, tasks, now, deadline=math.inf):
        pending = []
        for task in tasks:
            if not task.get('ticket'):
                match = re.search(r'sen-(\d+)', task.get('branch') or '', re.I)
                if match:
                    task['ticket'] = 'SEN-' + match[1]
            repo, sha = task.get('repo'), task.get('sha')
            if (isinstance(repo, str) and isinstance(sha, str)
                    and re.fullmatch(r'[\w.-]+/[\w.-]+', repo) and re.fullmatch('[0-9a-f]{40}', sha)):
                key = repo + '@' + sha
                known = self.commits.get(key)
                incomplete = not isinstance(known, dict) or 'branch' not in known or 'subject' not in known
                retry = known and any(not known.get(f) for f in ('ticket', 'branch', 'subject')) and now - (known.get('checked') or 0) >= 1800
                if (incomplete or retry) and key not in pending:
                    pending.append(key)
        pending = pending[:10]
        for index, key in enumerate(pending):
            if time.monotonic() >= deadline:
                pending = pending[:index]
                break
            repo, sha = key.split('@')
            found, branch, subject = None, None, None
            for path, field in (('branches-where-head', 'name'), ('pulls', 'head.ref')):
                if time.monotonic() >= deadline:
                    break
                try:
                    result = subprocess.run([self.gh, 'api', 'repos/' + repo + '/commits/' + sha + '/' + path, '--jq', '.[].' + field],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=min(5, max(.001, deadline - time.monotonic())), check=True)
                    if path == 'branches-where-head':
                        branch = next(iter(result.stdout.splitlines()), None)
                    match = re.search(r'sen-(\d+)', result.stdout, re.I)
                    found = 'SEN-' + match[1] if match else None
                except (OSError, subprocess.SubprocessError):
                    logging.warning('No se pudo consultar un commit en GitHub')
                if found:
                    break
            if time.monotonic() < deadline:
                try:
                    result = subprocess.run([self.gh, 'api', 'repos/' + repo + '/commits/' + sha, '--jq', '.commit.message'],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                        timeout=min(5, max(.001, deadline - time.monotonic())), check=True)
                    subject = next(iter(result.stdout.splitlines()), None)
                except (OSError, subprocess.SubprocessError):
                    logging.warning('No se pudo consultar un commit en GitHub')
            self.commits[key] = dict(ticket=found, branch=branch, subject=subject, checked=now)
        if pending:
            atomic(self.commit_path, json.dumps(self.commits))
        for task in tasks:
            if task.get('repo') and task.get('sha'):
                known = self.commits.get(task['repo'] + '@' + task['sha'], {})
                for field in ('ticket', 'branch', 'subject'):
                    if not task.get(field):
                        task[field] = known.get(field)

    def publish(self, now):
        hosts = []
        for host in self.hosts:
            ident = host['id']
            data = copy.deepcopy(self.good.get(ident))
            estimates = {}
            if data:
                data['tasks'] = [t for t in data['tasks'] if t['state'] != 'Done']
                for task in data['tasks']:
                    if task['state'] in ('Running', 'Queued'):
                        estimate = self.history.estimate(ident, task)
                        if estimate:
                            estimates[str(task['id'])] = estimate
            hosts.append(dict(id=ident, name=host['name'], fresh=data is not None and self.errors.get(ident) is None,
                              age=max(0, now - data['taken']) if data else None, error=self.errors.get(ident), snapshot=data, estimates=estimates))
        with self.lock:
            self.payload = dict(schema='rack@1', now=now, hosts=hosts,
                                tickets={k: {f: v.get(f) for f in ('title', 'project')} for k, v in self.tickets.items()})

    def cycle(self, fetch=fetch_host, now=None):
        now = time.time() if now is None else now
        with ThreadPoolExecutor(max_workers=len(self.hosts)) as pool:
            jobs = [(host, pool.submit(fetch, host)) for host in self.hosts]
            for host, job in jobs:
                ident = host['id']
                try:
                    data = validate_snapshot(job.result(), ident)
                    self.history.add(ident, data['tasks'], now)
                    self.good[ident] = data
                    self.errors[ident] = None
                except Exception as error:
                    self.errors[ident] = 'No se pudo leer el host: ' + type(error).__name__
        self.publish(now)
        deadline = time.monotonic() + LOOKUP_BUDGET
        self.resolve_commits([t for data in self.good.values() for t in data['tasks'] if t['state'] != 'Done'], now, deadline)
        self.refresh_tickets({t['ticket'] for data in self.good.values() for t in data['tasks'] if re.fullmatch(r'SEN-\d+', t.get('ticket') or '')}, now, deadline)
        self.publish(now)

    def read(self):
        with self.lock:
            return copy.deepcopy(self.payload)


def allowed_client(address, bind, funnel=False):
    if funnel:
        return False
    try:
        ip = ipaddress.ip_address(address)
        return ip in TAIL4 or ip in TAIL6 or (bind == '127.0.0.1' and address == '127.0.0.1')
    except ValueError:
        return False


class OwnerCheck:
    def __init__(self, owner, tailscale):
        self.owner = owner
        self.tailscale = tailscale
        self.cache = {}
        self.lock = threading.Lock()

    def allows(self, address):
        with self.lock:
            now = time.monotonic()
            cached = self.cache.get(address)
            if cached and now - cached[0] < 300:
                return cached[1]
            allowed = False
            try:
                # Bajo launchd, el binario de Tailscale.app sólo actúa como CLI si su padre es un shell.
                # Sesión propia para que el timeout termine también a whois, no sólo al shell.
                process = subprocess.Popen(['/bin/sh', '-c', '"$0" whois --json "$1"; exit $?', self.tailscale, address],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
                try:
                    output, _ = process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                    raise
                if process.returncode:
                    raise subprocess.CalledProcessError(process.returncode, 'whois')
                profile = json.loads(output)['UserProfile']
                allowed = bool(self.owner) and profile['LoginName'] == self.owner
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
                pass
            self.cache[address] = (now, allowed)
            return allowed


class Handler(BaseHTTPRequestHandler):
    def dispatch(self):
        if not allowed_client(self.client_address[0], self.server.server_address[0], 'Tailscale-Funnel-Request' in self.headers):
            self.reply(403, b'')
        elif not (self.client_address[0] == '127.0.0.1' and self.server.server_address[0] == '127.0.0.1') and not self.server.owner_check.allows(self.client_address[0]):
            self.reply(403, b'')
        elif self.command != 'GET':
            self.reply(405, b'', allow=True)
        elif self.path == '/':
            self.reply(200, (BASE / 'index.html').read_bytes(), 'text/html; charset=utf-8')
        elif self.path == '/api/rack.json':
            self.reply(200, json.dumps(self.server.rack.read(), allow_nan=False).encode(), 'application/json; charset=utf-8')
        else:
            self.reply(404, b'')

    def reply(self, status, body, content_type='text/plain', allow=False):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if allow:
            self.send_header('Allow', 'GET')
        self.end_headers()
        self.wfile.write(body)

    def __getattr__(self, name):
        if name.startswith('do_'):
            return self.dispatch
        raise AttributeError(name)

    def log_message(self, *args):
        pass


def bind_address(value):
    try:
        ip = ipaddress.ip_address(value)
        if value == '127.0.0.1' or ip in TAIL4:
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError('bind debe ser una IP de 100.64.0.0/10 o 127.0.0.1 para pruebas')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', required=True, type=bind_address)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--owner')
    parser.add_argument('--tailscale', default='/Applications/Tailscale.app/Contents/MacOS/Tailscale')
    parser.add_argument('--hosts', type=Path, default=BASE / 'hosts.json')
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/state/pao/rack')
    args = parser.parse_args()
    if args.bind != '127.0.0.1' and not args.owner:
        parser.error('--owner es obligatorio fuera de 127.0.0.1')
    try:
        hosts = load_hosts(args.hosts)
    except ValueError as error:
        parser.error(str(error))
    rack = Rack(hosts, args.state_dir)
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.rack = rack
    server.owner_check = OwnerCheck(args.owner, args.tailscale)
    stop = threading.Event()

    def collect():
        while not stop.is_set():
            started = time.monotonic()
            try:
                rack.cycle()
            except Exception:
                logging.exception('Falló el ciclo del rack')
            stop.wait(max(0, 15 - (time.monotonic() - started)))
    worker = threading.Thread(target=collect, daemon=True)
    worker.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == '__main__':
    main()
