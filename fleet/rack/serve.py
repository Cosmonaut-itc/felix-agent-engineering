#!/usr/bin/env python3
"""Dashboard del rack, sólo dentro del tailnet."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.request

BASE = Path(__file__).resolve().parent
# -I omits the script directory: load only our installed, fixed sibling.
_spec = importlib.util.spec_from_file_location('rack_accion', BASE / 'accion.py')
accion = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(accion)
_guard_spec = importlib.util.spec_from_file_location('rack_guard_snapshot', BASE / 'snapshot.py')
_guard_snapshot = importlib.util.module_from_spec(_guard_spec)
_guard_spec.loader.exec_module(_guard_snapshot)
TAIL4 = ipaddress.ip_network('100.64.0.0/10')
TAIL6 = ipaddress.ip_network('fd7a:115c:a1e0::/48')
TASK_STATES = ('Queued', 'Stashed', 'Locked', 'Running', 'Paused', 'Done')
TASK_TEXT = ('name', 'scope', 'project', 'ticket', 'repo', 'sha', 'result', 'branch', 'subject')
TASK_TIMES = ('enqueued', 'start', 'end')
LOOKUP_BUDGET = 8
REFRESH_TIMEOUT = 40
CYCLE_SECONDS, CYCLE_MIN_GAP = 15, 3
PULL_LIMIT, PULL_RETRY = 10, 1800


def load_tren():
    # -I omite el directorio del script. El módulo se usa sólo con Tren habilitado.
    spec = importlib.util.spec_from_file_location('rack_tren', BASE / 'tren.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
            if (not isinstance(host, dict) or not {'id', 'name', 'transport', 'argv'} <= set(host) or set(host) - {'id', 'name', 'transport', 'argv', 'actions'}
                    or not isinstance(host['id'], str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', host['id'])
                    or host['id'] in ids or not isinstance(host['name'], str) or not host['name']
                    or host['transport'] not in ('local', 'ssh') or not isinstance(host['argv'], list)
                    or not host['argv'] or any(not isinstance(a, str) or not a or '\0' in a for a in host['argv'])):
                raise ValueError()
            if 'actions' in host:
                # A local write channel acts on the panel's own host, never on one read over ssh.
                config = accion.action_config(host['actions'])
                if (config['transport'] == 'local' and host['transport'] != 'local'
                        or config['transport'] == 'ssh' and (host['id'] != 'desk' or host['transport'] != 'ssh')):
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


def valid_activity(value):
    if not isinstance(value, dict) or set(value) != {'last', 'kind', 'ref'}:
        return None
    last, kind, ref = value['last'], value['kind'], value['ref']
    if (last is not None and (isinstance(last, bool) or not isinstance(last, (int, float)) or isinstance(last, float) and not math.isfinite(last))
            or kind not in ('verify', 'chain', None)
            or ref is not None and (not isinstance(ref, str) or not 0 < len(ref) <= 200
                                    or any(ord(c) < 32 or ord(c) == 127 for c in ref))):
        return None
    return dict(last=last, kind=kind, ref=ref)


def valid_usage(value, host=False):
    fields = {'interval', 'busy', 'used', 'outside', 'docker'} if host else {'cpu', 'mem', 'containers'}
    if not isinstance(value, dict) or set(value) != fields:
        return None
    def number(n, integer=False):
        return (not isinstance(n, bool) and isinstance(n, int if integer else (int, float))
                and n >= 0 and (isinstance(n, int) or math.isfinite(n)))
    if host:
        outside = value['outside']
        if (not number(value['interval']) or not number(value['busy']) or not number(value['used'], True)
                or value['docker'] not in ('read', 'absent', 'error')
                or not isinstance(outside, dict) or set(outside) != {'cpu', 'mem'}
                or not number(outside['cpu']) or not number(outside['mem'], True)):
            return None
        return dict(interval=value['interval'], busy=value['busy'], used=value['used'],
                    docker=value['docker'], outside=dict(cpu=outside['cpu'], mem=outside['mem']))
    if not number(value['cpu']) or not number(value['mem'], True) or not number(value['containers'], True):
        return None
    return dict(cpu=value['cpu'], mem=value['mem'], containers=value['containers'])


def valid_orphans(data, host):
    if not {'orphans', 'orphans_total'} & set(data):
        return  # Compatibilidad con recolectores anteriores.
    rows, total = data.get('orphans'), data.get('orphans_total')
    if (host != 'desk' or type(rows) is not list or type(total) is not int or total < 0
            or len(rows) != min(total, 64)):
        raise ValueError('Huérfanos inválidos')
    keys = {'pid', 'comm', 'origin_task', 'group', 'origin_state', 'rss_bytes', 'cpu_percent', 'started_at'}
    seen, previous, clean = set(), math.inf, []
    for row in rows:
        if type(row) is not dict or not keys <= row.keys():
            raise ValueError('Campos de huérfano inválidos')
        if (type(row['pid']) is not int or row['pid'] <= 0 or row['pid'] in seen
                or type(row['comm']) is not str or len(row['comm']) > 32
                or (row['origin_task'] is not None and (type(row['origin_task']) is not str
                    or not re.fullmatch(r'[0-9]{1,20}', row['origin_task'])))
                or (row['group'] is not None and (type(row['group']) is not str or len(row['group']) > 128))
                or type(row['origin_state']) is not str or row['origin_state'] not in ('Done', 'Killed', 'Running', 'desconocido')
                or type(row['rss_bytes']) is not int or not 0 <= row['rss_bytes'] <= previous
                or (row['cpu_percent'] is not None and (type(row['cpu_percent']) not in (int, float)
                    or not math.isfinite(row['cpu_percent']) or row['cpu_percent'] < 0))
                or type(row['started_at']) is not str
                or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z', row['started_at'])):
            raise ValueError('Huérfano inválido')
        try:
            _guard_snapshot.epoch(row['started_at'])
        except ValueError:
            raise ValueError('Fecha de huérfano inválida') from None
        seen.add(row['pid'])
        previous = row['rss_bytes']
        clean.append({key: row[key] for key in keys})
    return dict(orphans=clean, orphans_total=total)


def capacity_fields(value, fields):
    if value is None:
        return None
    return {key: value[key] for key in fields
            if key in value and type(value[key]) in (int, float, type(None))}


def validate_snapshot(data, host):
    if not isinstance(data, dict) or data.get('schema') != 'rack-host@1' or data.get('host') != host:
        raise ValueError('Esquema o host inválido')
    if not isinstance(data.get('taken'), (float, int)) or not math.isfinite(data['taken']):
        raise ValueError('Fecha inválida')
    if not isinstance(data.get('tasks'), list) or not isinstance(data.get('groups'), list):
        raise ValueError('Tareas o grupos inválidos')
    clean = dict(schema='rack-host@1', host=host)
    if 'os' in data and isinstance(data['os'], (str, type(None))):
        clean['os'] = data['os']
    clean['taken'] = data['taken']
    for field, keys in (('cpu', ('cores', 'psi_avg60')),
                        ('mem', ('total', 'available', 'swap_total', 'swap_used')),
                        ('disk', ('total', 'free'))):
        if field in data:
            if not isinstance(data[field], (dict, type(None))):
                raise ValueError('Capacidad inválida')
            clean[field] = capacity_fields(data[field], keys)
    cpu = data.get('cpu')
    if cpu is not None and 'load' in cpu:
        load = cpu['load']
        if load is None:
            clean['cpu']['load'] = None
        elif isinstance(load, list) and all(type(n) in (int, float) for n in load):
            clean['cpu']['load'] = list(load)
    if 'guard' in data:
        clean['guard'] = _guard_snapshot.validate_guard(data['guard'], allow_extra=True)
    groups = []
    for group in data['groups']:
        if not isinstance(group, dict) or not isinstance(group.get('slots'), int) or not 0 <= group['slots'] <= 1000 or not isinstance(group.get('name'), str):
            raise ValueError('Grupo inválido')
        public = dict(name=group['name'])
        if 'status' in group and isinstance(group['status'], (str, type(None))):
            public['status'] = group['status']
        public['slots'] = group['slots']
        groups.append(public)
    if 'control' in data:
        control = data['control']
        if type(control) is dict:
            control = dict(control)
            group_names = {group['name'] for group in groups} - {'envs', 'command', 'original_command', 'path'}
            for field in ('pause_reasons', 'startup_holds', 'startup_waiting'):
                if type(control.get(field)) is dict:
                    control[field] = {k: v for k, v in control[field].items() if k in group_names}
        clean['control'] = accion.host_channel.control(control, host)
    orphans = valid_orphans(data, host)
    if orphans is not None:
        clean.update(orphans)
    clean['groups'] = groups
    clean['tasks'] = []
    for row in data['tasks']:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), int) or not isinstance(row.get('group'), str)
                or not isinstance(row.get('priority'), int) or isinstance(row.get('priority'), bool)
                or row.get('state') not in TASK_STATES
                or any(k not in row for k in TASK_TEXT + TASK_TIMES)
                or any(not isinstance(row.get(k), (str, type(None))) for k in TASK_TEXT)
                or any(not isinstance(row.get(k), (int, float, type(None))) for k in TASK_TIMES)):
            raise ValueError('Tarea inválida')
        public = {key: row[key] for key in ('id', 'group', 'priority', 'state') + TASK_TEXT + TASK_TIMES}
        if 'interruption_reason' in row:
            if (host not in ('desk', 'mini') or row['state'] != 'Done' or row.get('result') != 'Killed'
                    or row['interruption_reason'] != 'Interrumpida al reiniciar Pueue; relanzar manualmente'):
                raise ValueError('Motivo de interrupción inválido')
            public['interruption_reason'] = row['interruption_reason']
        if 'control' in row:
            public['control'] = accion.control_data(row['control'], row)
        if 'activity' in row:
            public['activity'] = valid_activity(row['activity'])
        if host == 'mini' and _guard_snapshot.valid_admission(row.get('admission'), row['id']):
            admission = row['admission']
            public['admission'] = {key: admission[key] for key in _guard_snapshot.ADMISSION_FIELDS}
            public['admission'].update(
                resources=list(admission['resources']),
                positions={name: admission['positions'][name] for name in admission['resources']},
                blockers=[{key: blocker[key] for key in _guard_snapshot.ADMISSION_BLOCKER_FIELDS}
                          for blocker in admission['blockers']])
        if 'usage' in row:
            public['usage'] = valid_usage(row['usage']) if row['state'] == 'Running' else None
        clean['tasks'].append(public)
    if 'errors' in data:
        errors = data['errors']
        if errors is not None and not isinstance(errors, list):
            raise ValueError('Errores inválidos')
        if any(isinstance(e, dict) and e.get('stage') == 'pueue' for e in errors or []):
            raise ValueError('Pueue no se pudo leer')
        clean['errors'] = None if errors is None else [
            {k: e[k] for k in ('stage', 'message') if k in e and isinstance(e[k], str)}
            for e in errors if isinstance(e, dict)]
    if 'usage' in data:
        clean['usage'] = valid_usage(data['usage'], host=True)
    json.dumps(clean, allow_nan=False)
    return clean


def fetch_host(host):
    argv = host['argv']
    if host['transport'] == 'local':
        argv = [a.replace('{rack_dir}', str(BASE)) for a in argv]
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=25, check=True)
    return validate_snapshot(json.loads(result.stdout), host['id'])


class Rack:
    def __init__(self, hosts, state_dir, key_path=None, gh='/opt/homebrew/bin/gh', tren_repos=()):
        self.hosts = hosts
        self.good = {}
        self.errors = {}
        self.history = History(state_dir)
        self.ticket_path = state_dir / 'tickets.json'
        self.tickets = read_cache(self.ticket_path, dict(title=str, project=str, updated=(int, float), failed=(int, float)))
        self.gh = gh
        self.commit_path = state_dir / 'commits.json'
        self.commits = read_cache(self.commit_path, dict(ticket=str, branch=str, subject=str, checked=(int, float)))
        self.tren_repos = tuple(tren_repos)
        self.tren = load_tren() if self.tren_repos else None
        self.tren_path = state_dir / 'tren.json'
        self.tren_good = None
        self.tren_error = None
        self.tren_present = False
        self.pull_path = state_dir / 'pulls.json'
        self.pulls = read_cache(self.pull_path, dict(ticket=str, title=str, checked=(int, float))) if self.tren else {}
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
        # Cada ciclo toma un número al empezar; read_hosts publica su lectura de hosts.
        self.started = self.read_hosts = 0
        self.published = threading.Condition(self.lock)
        self.wake = threading.Event()

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
                    branch = branch or next(iter(result.stdout.splitlines()), None)
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
            previous = self.commits.get(key) if isinstance(self.commits.get(key), dict) else {}
            self.commits[key] = dict(ticket=found or previous.get('ticket'), branch=branch or previous.get('branch'),
                                     subject=subject or previous.get('subject'), checked=now)
        if pending:
            atomic(self.commit_path, json.dumps(self.commits))
        for task in tasks:
            if task.get('repo') and task.get('sha'):
                known = self.commits.get(task['repo'] + '@' + task['sha'], {})
                for field in ('ticket', 'branch', 'subject'):
                    if not task.get(field):
                        task[field] = known.get(field)

    def read_tren(self, now):
        if not self.tren:
            return
        try:
            self.tren_good = self.tren.read_received(self.tren_path, self.tren_repos, now)
            self.tren_present, self.tren_error = True, None
        except FileNotFoundError:
            self.tren_present, self.tren_error, self.tren_good = False, None, None
        except (OSError, ValueError, RecursionError):
            self.tren_present, self.tren_error = True, 'No se pudo leer el tren'

    def tren_prs(self):
        prs = {}
        if self.tren_good is not None:
            for row in self.tren_good[1]['repos']:
                repo = row['repo']
                batch = row['lote']
                numbers = []
                if batch is not None:
                    numbers.extend(member['pr'] for member in batch['miembros'])
                    if batch['ensayo'] is not None:
                        numbers.extend(batch['ensayo']['prs'])
                pause = row['pausa']
                if pause is not None and pause['pr'] is not None:
                    numbers.append(pause['pr'])
                numbers.extend(member['pr'] for member in row['espera'])
                numbers.extend(member['pr'] for item in row['recientes'] for member in item['miembros'])
                for pr in numbers:
                    prs[f'{repo}#{pr}'] = (repo, pr)
        return prs

    def pull_fields(self, key):
        known = self.pulls.get(key, {})
        ticket = known.get('ticket') or ''
        return dict(ticket=ticket if re.fullmatch(r'SEN-[0-9]+', ticket) else None,
                    title=self.tren.public_title(known.get('title')))

    def resolve_pulls(self, prs, now, deadline):
        count = 0
        for key, (repo, pr) in prs.items():
            remaining = deadline - time.monotonic()
            if count >= PULL_LIMIT or remaining <= 0:
                break
            known = self.pull_fields(key)
            checked = self.pulls.get(key, {}).get('checked')
            if all(known.values()) or checked is not None and now - checked < PULL_RETRY:
                continue
            count += 1
            try:
                result = subprocess.run(
                    [self.gh, 'api', f'repos/{repo}/pulls/{pr}', '--jq', '{ref: .head.ref, title: .title}'],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                    timeout=min(5, remaining), check=True)
                pull = json.loads(result.stdout)
                if not isinstance(pull, dict) or not isinstance(pull.get('ref'), str):
                    raise ValueError('PR inválido')
                match = re.search(r'sen-([0-9]+)', pull['ref'], re.I)
                if match:
                    known['ticket'] = 'SEN-' + match[1]
                title = self.tren.public_title(pull.get('title'))
                if title is not None:
                    known['title'] = title
            except (OSError, subprocess.SubprocessError, ValueError, RecursionError):
                logging.warning('No se pudo consultar un PR del tren en GitHub')
            self.pulls[key] = dict(known, checked=now)
        if count:
            try:
                atomic(self.pull_path, json.dumps(self.pulls))
            except OSError:
                logging.warning('No se pudo guardar la caché de PRs del tren')

    @staticmethod
    def join_tren_run(run, tasks):
        place = run['lugar']
        host, task_id, ref = place['tipo'], place['pueue_id'], place['ref']
        entry = tasks.get((host, task_id))
        if entry is None:
            return
        task, estimate = entry
        if ref is not None:
            matches = (task.get('activity') or {}).get('ref') == ref
        else:
            start, begun = task.get('start'), run['inicio']
            matches = (host == 'mini' and run.get('fin') is None and start is not None
                       and begun is not None and start >= begun)
        if matches:
            rack = dict(host=host, id=task_id)
            run['rack'] = place['rack'] = rack
            # El mismo inicio que la tarjeta de host: la carga admitida o el arranque en Pueue, no la entrega al lanzador.
            admission = task.get('admission')
            run['rack_inicio'] = admission.get('payload_started_at') if admission else task.get('start')
            if estimate is not None:
                run['est'] = place['est'] = estimate

    def tren_view(self, hosts, now):
        age = None
        repos = []
        errors = []
        if self.tren_good is not None:
            received, data = self.tren_good
            age = max(0, now - received)
            repos = copy.deepcopy(data['repos'])
            errors = copy.deepcopy(data['errors'])
        tasks = {(host['id'], task['id']): (task, host['estimates'].get(str(task['id'])))
                 for host in hosts if host['id'] in ('desk', 'mini') and host['fresh']
                 for task in host['snapshot']['tasks'] if task['state'] != 'Done'}
        for row in repos:
            batch = row['lote']
            if batch is not None:
                for run in batch['suite']:
                    self.join_tren_run(run, tasks)
                trial = batch['ensayo']
                if trial is not None:
                    trial['unidad'] = self.tren.public_text(trial['unidad'])
                    self.join_tren_run(trial, tasks)
            for recent in row['recientes']:
                for member in recent['miembros']:
                    returned = member['devolucion']
                    if returned is not None:
                        returned['unidad'] = self.tren.public_text(returned['unidad'])
        return dict(fresh=age is not None and age <= self.tren.FRESH_SECONDS and self.tren_error is None,
                    age=age, error=self.tren_error, repos=repos, errors=errors,
                    prs={key: self.pull_fields(key) for key in self.tren_prs() if key in self.pulls})

    def publish(self, now):
        hosts = []
        for host in self.hosts:
            ident = host['id']
            data = copy.deepcopy(self.good.get(ident))
            estimates = {}
            if data:
                data['tasks'] = [t for t in data['tasks'] if t['state'] != 'Done' or t.get('interruption_reason')]
                for task in data['tasks']:
                    if task['state'] in ('Running', 'Queued'):
                        estimate = self.history.estimate(ident, task)
                        if estimate:
                            estimates[str(task['id'])] = estimate
            hosts.append(dict(id=ident, name=host['name'], actions=dict(reason=accion.transport_reason(host, 'remote-run')), fresh=data is not None and self.errors.get(ident) is None,
                              age=max(0, now - data['taken']) if data else None, error=self.errors.get(ident), snapshot=data, estimates=estimates))
        payload = dict(schema='rack@1', now=now, hosts=hosts,
                       tickets={k: {f: v.get(f) for f in ('title', 'project')} for k, v in self.tickets.items()})
        if getattr(self, 'tren_present', False):
            payload['tren'] = self.tren_view(hosts, now)
        with self.lock:
            self.payload = payload

    def cycle(self, fetch=fetch_host, now=None):
        now = time.time() if now is None else now
        with self.lock:
            self.started += 1
            number = self.started
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
        self.read_tren(now)
        self.publish(now)
        with self.published:
            self.read_hosts = number
            self.published.notify_all()
        deadline = time.monotonic() + LOOKUP_BUDGET
        self.resolve_commits([t for data in self.good.values() for t in data['tasks'] if t['state'] != 'Done'], now, deadline)
        self.refresh_tickets({t['ticket'] for data in self.good.values() for t in data['tasks'] if re.fullmatch(r'SEN-\d+', t.get('ticket') or '')}, now, deadline)
        if self.tren_present:
            # PR titles only take what commits and tickets leave of the shared budget.
            self.resolve_pulls(self.tren_prs(), now, deadline)
        self.publish(now)

    def read(self):
        with self.lock:
            return copy.deepcopy(self.payload)

    def refresh(self, timeout):
        """Despierta al recolector y devuelve la primera lectura que empezó después de la petición."""
        with self.published:
            target = self.started + 1
            self.wake.set()
            if not self.published.wait_for(lambda: self.read_hosts >= target, timeout):
                return None
            return copy.deepcopy(self.payload)


def allowed_client(address, bind, funnel=False):
    if funnel:
        return False
    try:
        ip = ipaddress.ip_address(address)
        return ip in TAIL4 or ip in TAIL6 or (bind == '127.0.0.1' and address == '127.0.0.1')
    except ValueError:
        return False


def allowed_host(headers, server):
    # Un nombre ajeno que resuelve a esta IP (DNS rebinding) haría same-origin la página de otro sitio.
    values = headers.get_all('Host') or []
    names = (server.server_address[0], *getattr(server, 'names', ()))
    return len(values) == 1 and values[0].lower() in {name.lower() + ':' + str(server.server_address[1]) for name in names}


class OwnerCheck:
    def __init__(self, owner, tailscale):
        self.owner = owner
        self.tailscale = tailscale
        self.cache = {}
        self.lock = threading.Lock()

    def allows(self, address, fresh=False):
        with self.lock:
            now = time.monotonic()
            cached = self.cache.get(address)
            if not fresh and cached and now - cached[0] < 300:
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
        with self.lock:
            self.cache[address] = (now, allowed)
        return allowed


class Handler(BaseHTTPRequestHandler):
    def action(self):
        started = self.server.actions.timer()
        # No loopback exception for writes: tests inject a fake owner check.
        host = self.headers.get('Host')
        origins = self.headers.get_all('Origin') or []
        if (not allowed_host(self.headers, self.server)
                or not allowed_client(self.client_address[0], self.server.server_address[0],
                                      'Tailscale-Funnel-Request' in self.headers)
                or (self.command == 'POST' and (origins != ['http://' + host]
                    or self.headers.get_all('X-Rack-Action') != ['1']
                    or self.headers.get_all('Content-Type') != ['application/json']
                    or self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin')))
                or not self.server.owner_check.allows(self.client_address[0], fresh=True)):
            self.action_reply(403, accion.result({}, 'rejected', 'forbidden'))
            return
        try:
            if self.path == '/api/accion' and self.command == 'POST':
                lengths = self.headers.get_all('Content-Length') or []
                if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 16384 or 'Transfer-Encoding' in self.headers:
                    raise ValueError()
                # Bound the entire body read, including a client sending one byte at a time.
                body_deadline = min(started + 8, self.server.actions.timer() + 5)
                raw = bytearray()
                size = int(lengths[0])
                while len(raw) < size:
                    remaining = body_deadline - self.server.actions.timer()
                    if remaining <= 0:
                        raise TimeoutError()
                    self.connection.settimeout(remaining)
                    chunk = self.rfile.read1(size - len(raw))
                    if not chunk:
                        raise ValueError()
                    raw.extend(chunk)
                code, response = self.server.actions.respond('post', accion.decode(raw), started=started)
            elif self.path.startswith('/api/accion/') and self.command == 'GET':
                code, response = self.server.actions.respond('get', self.path[len('/api/accion/'):], started=started)
            else:
                self.action_reply(405, accion.result({}, 'rejected', 'invalid_request'))
                return
            self.action_reply(code, response)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            self.action_reply(400, accion.result({}, 'rejected', 'invalid_request'))
        except (OSError, sqlite3.Error):
            # A failed receipt commit cannot imply that the launcher did nothing.
            self.action_reply(503, accion.result({}, 'unknown'))

    def action_reply(self, status, response):
        self.reply(status, json.dumps(response, allow_nan=False).encode(), 'application/json; charset=utf-8')

    def dispatch(self):
        if self.path == '/api/accion' or self.path.startswith('/api/accion/'):
            self.action()
            return
        if not allowed_host(self.headers, self.server):
            self.reply(403, b'')
        elif not allowed_client(self.client_address[0], self.server.server_address[0], 'Tailscale-Funnel-Request' in self.headers):
            self.reply(403, b'')
        elif not (self.client_address[0] == '127.0.0.1' and self.server.server_address[0] == '127.0.0.1') and not self.server.owner_check.allows(self.client_address[0]):
            self.reply(403, b'')
        elif self.path == '/api/refresh' and self.command == 'POST':
            # Un encabezado propio obliga al navegador a preguntar antes de enviarlo desde otro origen.
            payload = self.server.rack.refresh(REFRESH_TIMEOUT) if self.headers.get('X-Rack-Refresh') == '1' else False
            if payload is False:
                self.reply(403, b'')
            elif payload is None:
                self.reply(504, b'')
            else:
                self.reply(200, json.dumps(payload, allow_nan=False).encode(), 'application/json; charset=utf-8')
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
    parser.add_argument('--name', action='append', default=[], help='nombre DNS aceptado en Host, además de la IP de --bind')
    parser.add_argument('--tailscale', default='/Applications/Tailscale.app/Contents/MacOS/Tailscale')
    parser.add_argument('--hosts', type=Path, default=BASE / 'hosts.json')
    parser.add_argument('--config', type=Path, help='rack.json opcional con tren.repos')
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/state/pao/rack')
    args = parser.parse_args()
    if args.bind != '127.0.0.1' and not args.owner:
        parser.error('--owner es obligatorio fuera de 127.0.0.1')
    try:
        hosts = load_hosts(args.hosts)
        config = args.config or BASE / 'rack.json'
        tren_repos = load_tren().load_repos(config) if args.config or config.exists() else ()
    except (OSError, ValueError, RecursionError) as error:
        parser.error(str(error))
    rack = Rack(hosts, args.state_dir, tren_repos=tren_repos)
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.rack = rack
    server.names = tuple(args.name)
    server.owner_check = OwnerCheck(args.owner, args.tailscale)
    server.actions = accion.Actions(hosts, args.state_dir, rack.read)
    stop = threading.Event()

    def collect():
        while not stop.is_set():
            started = time.monotonic()
            try:
                rack.cycle()
            except Exception:
                logging.exception('Falló el ciclo del rack')
            rack.wake.wait(max(0, CYCLE_SECONDS - (time.monotonic() - started)))
            rack.wake.clear()
            stop.wait(max(0, CYCLE_MIN_GAP - (time.monotonic() - started)))
    worker = threading.Thread(target=collect, daemon=True)
    worker.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        rack.wake.set()
        server.server_close()


if __name__ == '__main__':
    main()
