#!/usr/bin/env python3
"""Instantánea saneada del host; compatible con Python 3.9."""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import ctypes
import datetime
import json
import hashlib
import math
import os
from pathlib import Path
import re
import subprocess
import sqlite3
import stat
import sys
import threading
import time
import uuid


def command(argv, timeout=5):
    return subprocess.run(argv, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, timeout=timeout,
                          env=dict(os.environ, LC_ALL='C')).stdout


def epoch(value):
    if isinstance(value, (int, float)):
        return value
    return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() if value else None


def ticket(text):
    match = re.search(r'sen-(\d+)', text, re.I)
    return 'SEN-' + match[1] if match else None


# Etiqueta de Pueue del remote-run de Bodega (repo apotheca): su ruta de corrida es la misma que la de Hasito.
BODEGA = 'apotheca-remote-run'
REMOTE_RUN_REF = re.compile(r'[0-9]{8}-[0-9]{6}-[0-9a-f]{6}')


# resource-state@1, publicado por scripts/mini/resource_admission.py de Hasito.
ADMISSION_PATH = '.local/state/comprador/mini/admission-v1/resource-state.json'
ADMISSION_BYTES = 1024 * 1024
ADMISSION_STALE = 30
ADMISSION_ROWS = 4096
ADMISSION_RESOURCES = 64
ADMISSION_TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}')
ADMISSION_RESOURCE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,63}')
ADMISSION_PHASES = ('waiting', 'reserved', 'claimed', 'running', 'draining', 'done',
                    'cancelled', 'expired', 'unknown')
ADMISSION_FIELDS = frozenset(('task_id', 'task_created_at', 'ref', 'daemon_generation', 'attempt',
                             'seq', 'phase', 'arrived_at', 'deadline_at', 'updated_at',
                             'payload_started_at', 'resources', 'positions', 'blockers'))
ADMISSION_BLOCKER_FIELDS = frozenset(('resource', 'kind', 'task_id', 'ref', 'seq'))


def admission_token(value):
    return isinstance(value, str) and ADMISSION_TOKEN.fullmatch(value) is not None


def admission_resource(value):
    return isinstance(value, str) and ADMISSION_RESOURCE.fullmatch(value) is not None


def admission_int(value, minimum=0):
    return type(value) is int and value >= minimum


def admission_time(value):
    return type(value) in (int, float) and math.isfinite(value)


def admission_blockers(items, resources):
    if not isinstance(items, list) or len(items) > ADMISSION_ROWS * ADMISSION_RESOURCES:
        return False
    counts = dict.fromkeys(resources, 0)
    for item in items:
        if (not isinstance(item, dict) or item.keys() != ADMISSION_BLOCKER_FIELDS
                or not admission_resource(item['resource']) or item['resource'] not in counts
                or item['kind'] not in ('holder', 'predecessor', 'legacy', 'unknown')
                or (item['task_id'] is not None and not admission_int(item['task_id']))
                or (item['ref'] is not None and not admission_token(item['ref']))
                or (item['seq'] is not None and not admission_int(item['seq'], 1))
                or (item['kind'] == 'unknown' and any(item[k] is not None for k in ('task_id', 'ref', 'seq')))):
            return False
        counts[item['resource']] += 1
    return all(count <= ADMISSION_ROWS for count in counts.values())


def valid_admission(row, task_id):
    """Forma que puede publicar el snapshot del rack, sin diagnóstico de corte."""
    try:
        if not isinstance(row, dict) or row.keys() != ADMISSION_FIELDS:
            return False
        resources = row['resources']
        positions = row['positions']
        return (admission_int(row['task_id']) and row['task_id'] == task_id and task_id < 2**63
                and all(admission_token(row[k]) for k in ('task_created_at', 'ref', 'daemon_generation'))
                and isinstance(row['attempt'], str) and str(uuid.UUID(row['attempt'])) == row['attempt']
                and admission_int(row['seq'], 1) and row['phase'] in ADMISSION_PHASES
                and all(admission_time(row[k]) for k in ('arrived_at', 'deadline_at', 'updated_at'))
                and (row['payload_started_at'] is None or admission_time(row['payload_started_at']))
                and isinstance(resources, list) and 1 <= len(resources) <= ADMISSION_RESOURCES
                and all(admission_resource(n) and n != 'ninguno' for n in resources)
                and len(set(resources)) == len(resources)
                and isinstance(positions, dict) and positions.keys() == set(resources)
                and all(p is None or admission_int(p, 1) for p in positions.values())
                and admission_blockers(row['blockers'], resources))
    except (ValueError, OverflowError):
        return False


def valid_admission_cut(cut, resources):
    if cut is None:
        return True
    times = {'limite_s', 'transcurrido_s', 'sin_salida_s', 'blockers_updated_at', 'espera_s'}
    return (isinstance(cut, dict) and cut.keys() == times | {
                'motivo', 'grupo', 'fase', 'recursos', 'bloqueantes', 'payload_started', 'no_payload'}
            and cut['motivo'] == 'espera-candados'
            and cut['grupo'] in ('suites', 'hasito', 'otros', 'rapidas', 'pesadas', 'jale')
            and cut['fase'] in ('espera-recurso', 'espera-lugar') and cut['recursos'] == resources
            and cut['payload_started'] is False and cut['no_payload'] is True
            and all(admission_time(cut[k]) and cut[k] >= 0 for k in times)
            and admission_blockers(cut['bloqueantes'], resources))


def read_owned_file(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('Archivo de admisión inválido')
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('Archivo de admisión demasiado grande')
        return raw


def admission_json_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError('Clave de admisión repetida')
    return value


def admission_json_constant(_):
    raise ValueError('Constante de admisión inválida')


def read_admission(home, now):
    """La proyección es opcional. Un fallo devuelve la vista nativa."""
    try:
        raw = read_owned_file(home / ADMISSION_PATH, ADMISSION_BYTES).decode('utf-8')
        state = json.loads(raw, object_pairs_hook=admission_json_object, parse_constant=admission_json_constant)
        if (not isinstance(state, dict) or state.keys() != {
                'schema', 'revision', 'updated_at', 'status', 'migration', 'requests'}
                or state['schema'] != 'resource-state@1' or state['status'] != 'ok'
                or not admission_int(state['revision']) or not admission_time(state['updated_at'])
                or not 0 <= now - state['updated_at'] <= ADMISSION_STALE):
            return []
        migration = state['migration']
        if (not isinstance(migration, dict) or migration.keys() != {'phase', 'barriers'}
                or migration['phase'] not in ('legacy-drain', 'grants')
                or not isinstance(migration['barriers'], list) or len(migration['barriers']) > ADMISSION_ROWS):
            return []
        for barrier in migration['barriers']:
            if (not isinstance(barrier, dict) or barrier.keys() != {'resource', 'task_ids'}
                    or not admission_resource(barrier['resource']) or not isinstance(barrier['task_ids'], list)
                    or len(barrier['task_ids']) > ADMISSION_ROWS or not all(map(admission_int, barrier['task_ids']))):
                return []
        if not isinstance(state['requests'], list) or len(state['requests']) > ADMISSION_ROWS:
            return []
        rows, seen = [], set()
        for row in state['requests']:
            if not isinstance(row, dict) or row.keys() != ADMISSION_FIELDS | {'cut'}:
                return []
            public = {k: v for k, v in row.items() if k in ADMISSION_FIELDS}
            if (not valid_admission(public, row['task_id']) or row['seq'] in seen
                    or not valid_admission_cut(row['cut'], row['resources'])):
                return []
            rows.append(public)
            seen.add(row['seq'])
        return rows
    except (OSError, ValueError, OverflowError, RecursionError):
        return []


class AdmissionBSDInfo(ctypes.Structure):
    # Darwin proc_bsdinfo, igual a resource_admission_host.py en Hasito.
    _fields_ = [(name, ctypes.c_uint32) for name in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid',
        'rgid', 'svuid', 'svgid', 'rfu')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in ('nfiles', 'pgid', 'pjobc', 'e_tdev', 'e_tpgid', 'nice')]
    _fields_ += [('sec', ctypes.c_uint64), ('usec', ctypes.c_uint64)]


def admission_generation(home):
    """Identidad del daemon que Hasito usa en resource-state@1."""
    try:
        raw = read_owned_file(home / 'Library/Application Support/pueue/pueue.pid', 32)
        if not re.fullmatch(rb'[1-9][0-9]*\s*', raw) or int(raw) >= 2**31:
            return None
        pid = int(raw)
        info = AdmissionBSDInfo()
        library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        proc_pidinfo = library.proc_pidinfo
        proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
        proc_pidinfo.restype = ctypes.c_int
        size = ctypes.sizeof(info)
        if (proc_pidinfo(pid, 3, 0, ctypes.byref(info), size) != size or info.pid != pid
                or info.status == 5 or info.comm != b'pueued'):
            return None
        boot = str(uuid.UUID(command(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'], timeout=.5).strip()))
        return 'pueued-' + hashlib.sha256(json.dumps([boot, pid, [info.sec, info.usec]]).encode()).hexdigest()
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return None


def admission_label(task):
    label = task.get('label')
    if not isinstance(label, str):
        return None
    names = [part[4:] for part in label.split() if part.startswith('run=')]
    return names[0] if len(names) == 1 and admission_token(names[0]) else None


def match_admission(tasks, rows, generation):
    managed = {}
    for row in rows:
        task_id = row['task_id']
        task = tasks.get(str(task_id))
        if (row['daemon_generation'] == generation and task and type(task['id']) is int
                and task['id'] == task_id and task.get('created_at') == row['task_created_at']
                and admission_label(task) == row['ref']
                and (task_id not in managed or managed[task_id]['seq'] < row['seq'])):
            managed[task_id] = row
    return managed


def mini_metadata(task, home=None):
    meta = {}
    with (Path(task['path']).expanduser() / 'meta').open() as stream:
        for line in stream:
            key, sep, value = line.rstrip('\n').partition('=')
            # remote-run escribe cmd= al final y puede ocupar varias líneas: nada después se lee.
            if key == 'cmd':
                break
            if sep and key in ('src', 'mirror'):
                meta.setdefault(key, value)
    branch, subject = None, None
    mirror = meta.get('mirror')
    if mirror:
        mirror = (home or Path.home()) / mirror
        for field, args in (('branch', ['branch', '--show-current']), ('subject', ['log', '-1', '--format=%s'])):
            try:
                value = command(['git', '-C', str(mirror)] + args).strip() or None
            except (OSError, subprocess.SubprocessError):
                value = None
            if field == 'branch':
                branch = value
            else:
                subject = value
    name = re.sub(r'-[a-fA-F0-9]{8}$', '', Path(meta.get('mirror', '')).name)
    name = name or (branch.rsplit('/', 1)[-1] if branch else None)
    src = meta.get('src', '')
    match = re.search(r'(?:\.t3/worktrees/|REPOS/)([^/.]+)', src)
    scope = (name or '').split('-')[0]
    return dict(name=name or None, scope=scope if scope in ('suite', 'dirigida', 'repro', 'diag') else None,
                project='apotheca' if task.get('label') == BODEGA else match[1] if match else None, ticket=ticket((name or '') + ' ' + src), branch=branch, subject=subject)


def desk_metadata(task, home, cache):
    match = re.fullmatch(r'exec bash (/.*/job-[^/\s]+/task\.sh)', task.get('command', ''))
    if not match:
        return {}
    values = {}
    with Path(match[1]).with_name('body.sh').open() as stream:
        for line in stream:
            found = re.fullmatch(r'declare -- (MODE|SLOT_KIND|BASE_REPO|REQUESTED_SHA|BRANCH)="([^"\n]*)"\n?', line)
            if found:
                values[found[1]] = found[2]
    kind = 'verify' if values.get('MODE') == 'launch' and values.get('SLOT_KIND') == 'verify' else 'chain'
    branch = values.get('BRANCH')
    branch = branch if branch and not re.fullmatch(r'[0-9a-fA-F]{40}', branch) else None
    repo = Path(values.get('BASE_REPO', '/')).resolve()
    if home.resolve() not in repo.parents:
        return dict(name=values.get('MODE'), branch=branch, activity=dict(last=None, kind=kind, ref=Path(match[1]).parent.name))
    if repo not in cache:
        try:
            url = command(['git', '-C', str(repo), 'config', '--get', 'remote.origin.url']).strip()
        except (OSError, subprocess.SubprocessError):
            url = ''
        found = re.search(r'github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$', url)
        cache[repo] = found[1] if found else None
    sha = values.get('REQUESTED_SHA', '').lower()
    return dict(name=values.get('MODE'), project=repo.name, repo=cache[repo],
                sha=sha if re.fullmatch('[0-9a-f]{40}', sha) else None, branch=branch,
                activity=dict(last=None, kind=kind, ref=Path(match[1]).parent.name))


def task_activity(task, mode, home, metadata, active=True, managed=False):
    """Sólo stat de salidas conocidas; nunca se abre un log."""
    activity = dict(metadata.get('activity') or dict(last=None, kind=None, ref=None))
    jale = None if managed else re.fullmatch(r'exec bash (/.*/job-[^/\s]+/task\.sh)', task.get('command', ''))
    run = Path(task.get('path', '')).expanduser()
    logs = [home / ('Library/Application Support/pueue' if mode == 'macos' else '.local/share/pueue')
            / 'task_logs' / (str(task['id']) + '.log')]
    if jale:
        job = Path(jale[1]).parent
        activity['ref'] = job.name
        activity['kind'] = activity['kind'] or 'chain'
        logs.append(job / 'log')
    elif run.parent == home / 'remote-run/runs' and (not managed or REMOTE_RUN_REF.fullmatch(run.name)):
        activity['ref'] = run.name
        logs.append(run / 'log')
    if active:
        times = []
        for path in logs:
            try:
                times.append(path.stat().st_mtime)
            except OSError:
                continue
        activity['last'] = max(times) if times else None
    return activity


def task_control(task, home, state):
    """Identity from queue fields only. Capability remains the launcher's decision."""
    launcher, ref, reason = None, None, None
    run = Path(task.get('path', '')).expanduser()
    if task.get('label') == BODEGA:
        reason = 'Las corridas de Bodega todavía no se cancelan desde el panel.'
    elif run.parent == home / 'remote-run/runs' and REMOTE_RUN_REF.fullmatch(run.name):
        launcher, ref = 'remote-run', run.name
    else:
        match = re.fullmatch(r'exec bash (/.*/job-[A-Za-z0-9]{8}/task\.sh)', task.get('command', ''))
        job = Path(match[1]).parent if match else None
        if job and job.parent == home / '.local/state/jale/pueue':
            launcher, ref = 'jale', job.name
    if launcher is None:
        reason = reason or 'Lanzador no reconocido; no se puede confirmar la cancelación.'
    elif state not in ('Queued', 'Running'):
        reason = 'Sólo se cancelan corridas en fila o en curso.'
    return dict(launcher=launcher, ref=ref,
                expected=dict(task_id=task['id'], state=state) if state in ('Queued', 'Running') else None,
                reason=reason)


def desk_interruptions(home, host='desk'):
    # Same authority, read-only. Never infer cancellation from Killed or a log.
    path = home / ('.local/state/pao/pueue-mini/control.sqlite3' if host == 'mini' else '.local/state/pao/pueue-desk/control.sqlite3')
    if not os.path.lexists(path):
        return {}
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.2)
    try:
        row = db.execute('SELECT body FROM policy WHERE id=1').fetchone()
        return json.loads(row[0]).get('startup', {}).get('interruptions', {})
    finally:
        db.close()


def mini_reorder_ages(home):
    """Only timestamps/opaque fingerprints from the same read-only authority."""
    path=home/'.local/state/pao/pueue-mini/control.sqlite3'
    if not path.is_file():
        return {}
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2)
    try:
        links={}
        for body, in db.execute('SELECT body FROM host_actions'):
            action=json.loads(body)
            if action.get('kind')!='reorder':
                continue
            for item in action.get('recovered_enqueued',{}).values():
                if (type(item) is not dict or set(item)!={'identity','before','after'}
                        or not re.fullmatch('[0-9a-f]{64}',item.get('identity',''))):
                    raise ValueError('Edad de fila inválida')
                epoch(item['before']);epoch(item['after'])
                links[(item['identity'],item['after'])]=item['before']
        return links
    finally:
        db.close()


def original_queue_age(task, stamp, links):
    fields=('id','created_at','group','command','original_command','path','envs','dependencies','label')
    identity=hashlib.sha256(json.dumps({k:task[k] for k in fields},sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    seen=set()
    while (identity,stamp) in links and stamp not in seen:
        seen.add(stamp);stamp=links[(identity,stamp)]
    return epoch(stamp)


GUARD_CAUSES = {'fresh_sample','docker','generation','desired','event','admission','startup','authority',
                'maintenance','reason','pressure_high','dependency','quota','task_changed','cooldown',
                'admission_unknown','clock','log','rpc'}
GUARD_EVENTS = {'admision_una','tope_alcanzado','retenido','guard_fin'}


def guard_number(value, maximum=None):
    return type(value) in (int,float) and math.isfinite(value) and value >= 0 and (maximum is None or value <= maximum)


def guard_date(value):
    if type(value) is not str or len(value) > 40 or not value.endswith(('Z','+00:00')):
        raise ValueError('Fecha de guardia inválida')
    stamp = epoch(value)
    if not math.isfinite(stamp):
        raise ValueError('Fecha de guardia inválida')
    return stamp


def guard_identity(row):
    if (type(row) is not dict or set(row) != {'at','grupo','id'} or type(row['id']) is not int
            or not 0 <= row['id'] < 2**64 or row['grupo'] not in ('hasito','jale','jale-verify','otros','pesadas')):
        raise ValueError('Identidad de admisión inválida')
    guard_date(row['at'])
    return dict(row)


def validate_guard(value):
    """Project validated guard data, including optional fields from older senders."""
    if value is None:
        return None
    if type(value) is not dict:
        raise ValueError('Guardia inválida')
    if value.get('action') not in (None,'pausar','mantener','reanudar'):
        raise ValueError('Decisión inválida')
    if value.get('at') is not None:guard_date(value['at'])
    for k in ('cpu_pause','cpu_resume'):
        if value.get(k) is not None and not guard_number(value[k],100):raise ValueError('Umbral CPU inválido')
    if 'status' in value and value['status'] not in ('fresh','stale','incomplete','unknown'):
        raise ValueError('Vigencia inválida')
    clean = {k: value[k] for k in ('action', 'at', 'cpu_pause', 'cpu_resume', 'status')
             if k in value}
    clean.update({k: None for k in ('pressure', 'thresholds', 'last_result', 'last_admission')
                  if k in value})
    pressure = value.get('pressure')
    if pressure is not None:
        fields = {'at', 'cpu', 'io', 'memory'}
        if type(pressure) is not dict or not fields <= pressure.keys():
            raise ValueError('Presión inválida')
        guard_date(pressure['at'])
        if not all(guard_number(pressure[k],None if k=='memory' else 100) for k in ('cpu','io','memory')):
            raise ValueError('Presión inválida')
        clean['pressure'] = {k: pressure[k] for k in fields}
    thresholds = value.get('thresholds')
    if thresholds is not None:
        if type(thresholds) is not dict or not {'normal', 'rapidas'} <= thresholds.keys():
            raise ValueError('Umbrales inválidos')
        clean['thresholds'] = {}
        for group in ('normal','rapidas'):
            item=thresholds[group]
            if type(item) is not dict or not {'pause', 'resume'} <= item.keys():
                raise ValueError('Umbrales inválidos')
            limits = {}
            for phase in ('pause','resume'):
                row=item[phase];keys={'cpu','io','memory'} if group=='normal' else {'memory'}
                if type(row) is not dict or not keys <= row.keys() or not all(guard_number(row[k],None if k=='memory' else 100) for k in keys):
                    raise ValueError('Umbrales inválidos')
                limits[phase] = {k: row[k] for k in keys}
            if limits['pause']['memory'] > limits['resume']['memory'] or any(limits['pause'][k] <= limits['resume'][k] for k in ('cpu','io') if k in limits['pause']):
                raise ValueError('Histéresis inválida')
            clean['thresholds'][group] = limits
    identity = value.get('last_admission')
    if identity is not None:
        fields = {'at', 'grupo', 'id'}
        if type(identity) is not dict or not fields <= identity.keys():
            raise ValueError('Identidad de admisión inválida')
        clean['last_admission'] = guard_identity({k: identity[k] for k in fields})
    result=value.get('last_result')
    if result is not None:
        if (type(result) is not dict or not {'evento','tick_id','at','bloqueos'} <= result.keys()
                or result['evento'] not in GUARD_EVENTS or type(result['tick_id']) is not str
                or not re.fullmatch('[0-9a-f]{24}',result['tick_id']) or type(result['bloqueos']) is not list
                or len(result['bloqueos'])>16):raise ValueError('Resultado inválido')
        guard_date(result['at'])
        blockers = []
        fields = {'grupo', 'operacion', 'causa'}
        for row in result['bloqueos']:
            if (type(row) is not dict or not fields <= row.keys() or row['causa'] not in GUARD_CAUSES
                    or row['operacion'] not in ('one','resume','unknown')
                    or row['grupo'] not in (None,'hasito','jale','jale-verify','otros','pesadas','rapidas')):
                raise ValueError('Bloqueo inválido')
            blockers.append({k: row[k] for k in fields})
        clean['last_result'] = dict(evento=result['evento'], tick_id=result['tick_id'],
                                    at=result['at'], bloqueos=blockers)
    return clean


def installed_thresholds(home):
    source = home / '.local/lib/pao-pueue-desk/control.py'
    def literal(node, depth=0):
        if depth>8:raise ValueError('AST demasiado profundo')
        if isinstance(node,ast.Constant) and type(node.value) in (int,float,str):return node.value
        if isinstance(node,ast.Dict):return {literal(k,depth+1):literal(v,depth+1) for k,v in zip(node.keys,node.values)}
        if isinstance(node,ast.BinOp) and isinstance(node.op,(ast.Mult,ast.Pow)):
            a,b=literal(node.left,depth+1),literal(node.right,depth+1)
            if not guard_number(a,2**40) or not guard_number(b,2**40) or isinstance(node.op,ast.Pow) and b>40:
                raise ValueError('AST numérico inválido')
            return a*b if isinstance(node.op,ast.Mult) else a**b
        raise ValueError('AST no permitido')
    tree=ast.parse(source.read_text())
    values=[literal(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='PRESSURE_THRESHOLDS' for t in n.targets)]
    if len(values)!=1:raise ValueError('Faltan umbrales instalados')
    return values[0]


def current_pressure(proc, now):
    result=dict(at=datetime.datetime.fromtimestamp(now,datetime.timezone.utc).isoformat())
    for name,kind in (('cpu','some'),('io','full')):
        lines=(proc/'pressure'/name).read_text().splitlines()
        line=next(s for s in lines if s.split()[0]==kind)
        result[name]=float(dict(p.split('=',1) for p in line.split()[1:])['avg60'])
    mem=dict(line.split(':',1) for line in (proc/'meminfo').read_text().splitlines())
    amount,unit=mem['MemAvailable'].split()
    if unit!='kB':raise ValueError('Memoria inválida')
    result['memory']=int(amount)*1024
    validate_guard(dict(pressure=result))
    return result


def durable_admission(home):
    path=home/'.local/state/pao/pueue-desk/control.sqlite3'
    if not os.path.lexists(path):return None
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2)
    try:
        if not db.execute("SELECT name FROM sqlite_master WHERE name='guard_admission' AND type='table'").fetchone():return None
        rows=db.execute('SELECT id,body FROM guard_admission').fetchall()
        if len(rows)!=1 or rows[0][0]!=1:raise ValueError('Permiso inválido')
        value=json.loads(rows[0][1])
        if type(value) is not dict or set(value) != {'version','last_attempt','last_confirmed'} or type(value['version']) is not int or value['version']!=1:
            raise ValueError('Permiso inválido')
        attempt=value['last_attempt'];confirmed=value['last_confirmed']
        if confirmed is not None:guard_identity(confirmed)
        if attempt is None:
            if confirmed is not None:raise ValueError('Confirmación sin intento')
        else:
            fields={'boot_id','reserved_ns','completed_ns','at','generation','grupo','id','created_at','enqueued_at','outcome'}
            if type(attempt) is not dict or set(attempt)!=fields:raise ValueError('Intento inválido')
            guard_identity({k:attempt[k] for k in ('at','grupo','id')})
            for k in ('created_at','enqueued_at'):
                if type(attempt[k]) is not str or epoch(attempt[k]) is None:raise ValueError('Identidad inválida')
            if (type(attempt['reserved_ns']) is not int or attempt['reserved_ns']<0
                    or type(attempt['boot_id']) is not str or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',attempt['boot_id'])
                    or type(attempt['generation']) is not str or not re.fullmatch(r'linux-proc-v1:[0-9a-f]{64}',attempt['generation'])
                    or attempt['outcome'] not in ('reserved','unknown','confirmed','not_sent')):raise ValueError('Intento inválido')
            end=attempt['completed_ns']
            if ((attempt['outcome'] in ('confirmed','not_sent')) != (end is not None)
                    or end is not None and (type(end) is not int or end<attempt['reserved_ns'])
                    or attempt['outcome']=='confirmed' and (confirmed is None or any(confirmed[k]!=attempt[k] for k in ('grupo','id')))):
                raise ValueError('Finalización inválida')
        return confirmed
    finally:db.close()


def desk_guard(home, proc=Path('/proc'), now=None, errors=None):
    now=time.time() if now is None else now
    def attempt(stage,fn):
        try:return fn()
        except Exception as error:
            if errors is not None:errors.append(dict(stage=stage,message='Lectura fallida: '+type(error).__name__))
            return None
    normal=attempt('guard_thresholds',lambda:installed_thresholds(home))
    thresholds=None
    if normal is not None:
        try:
            thresholds=dict(normal=normal,rapidas={k:dict(memory=normal[k]['memory']) for k in ('pause','resume')})
            validate_guard(dict(thresholds=thresholds))
        except (KeyError,ValueError,TypeError):
            thresholds=None
            if errors is not None:errors.append(dict(stage='guard_thresholds',message='Umbrales inválidos'))
    pressure=attempt('guard_pressure',lambda:current_pressure(proc,now))
    admission=attempt('guard_admission',lambda:durable_admission(home))
    last,result=None,None
    directory=home/'.local/state/pao/pueue-desk'
    for name in ('guardia.log.1','guardia.log'):
        path=directory/name
        try:
            with path.open('rb') as stream:
                stream.seek(max(0,path.stat().st_size-1024*1024))
                lines=stream.read(1024*1024+4096).splitlines()
        except FileNotFoundError:continue
        for line in lines:
            if len(line)>4096:continue
            try:
                row=json.loads(line)
                if row.get('evento')=='decision' and row.get('accion') in ('pausar','mantener','reanudar'):
                    guard_date(row['utc']);last=row;result=None
                elif last and row.get('evento') in GUARD_EVENTS and row.get('tick_id')==last.get('tick_id'):
                    candidate={k:row[k] for k in ('evento','tick_id','bloqueos')};candidate['at']=row['utc']
                    validate_guard(dict(last_result=candidate));result=candidate
            except (ValueError,KeyError,TypeError):continue
    status='unknown' if not last else 'stale' if not 0<=now-guard_date(last['utc'])<=120 else 'fresh' if result else 'incomplete'
    if status!='fresh':result=None
    # A new sensor sample cannot turn a prior tick's blockers into a current gate.
    if result and pressure and thresholds:
        high,low=thresholds['normal']['pause'],thresholds['normal']['resume']
        action=('pausar' if pressure['memory']<high['memory'] or any(pressure[k]>high[k] for k in ('cpu','io')) else
                'reanudar' if pressure['memory']>=low['memory'] and all(pressure[k]<low[k] for k in ('cpu','io')) else 'mantener')
        if action!=last['accion']:result=None;status='incomplete'
    def cpu(phase):
        value=normal.get(phase,{}).get('cpu') if type(normal) is dict else None
        return value if guard_number(value,100) else None
    return validate_guard(dict(action=last['accion'] if last else None,at=last['utc'] if last else None,
        cpu_pause=cpu('pause'),cpu_resume=cpu('resume'),pressure=pressure,thresholds=thresholds,
        last_result=result,last_admission=admission,status=status))

def mini_guard():
    return None


def disk(path):
    stat = os.statvfs(path)
    return dict(total=stat.f_blocks * stat.f_frsize, free=stat.f_bavail * stat.f_frsize)


def linux_capacity(proc):
    load = [float(x) for x in (proc / 'loadavg').read_text().split()[:3]]
    mem = {}
    for line in (proc / 'meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        mem[key] = int(value.split()[0]) * 1024
    return dict(cores=os.cpu_count(), load=load, psi_avg60=None), dict(total=mem['MemTotal'], available=mem['MemAvailable'], swap_total=mem['SwapTotal'], swap_used=mem['SwapTotal'] - mem['SwapFree'])


def mac_capacity():
    raw = command(['/usr/sbin/sysctl', '-n', 'hw.ncpu', 'hw.memsize', 'vm.loadavg', 'vm.swapusage']).splitlines()
    cores, total = int(raw[0]), int(raw[1])
    load = [float(x) for x in re.findall(r'[\d.]+', raw[2])]
    swap = {k: float(v) * 1024**2 for k, v in re.findall(r'(total|used) = ([\d.]+)M', raw[3])}
    vm = command(['/usr/bin/vm_stat'])
    page = int(re.search(r'page size of (\d+) bytes', vm)[1])
    available = sum(int(re.search(r'Pages ' + key + r':\s+(\d+)', vm)[1]) for key in ('free', 'inactive', 'speculative')) * page
    return dict(cores=cores, load=load, psi_avg60=None), dict(total=total, available=available, swap_total=swap['total'], swap_used=swap['used'])


def linux_processes(proc, deadline=None):
    """Una lectura de contadores; ningún texto privado entra al resultado público."""
    lines = (proc / 'stat').read_text().splitlines()
    ticks = [int(n) for n in lines[0].split()[1:9]]
    boot = next((int(line.split()[1]) for line in lines if line.startswith('btime ')), None)
    hertz = os.sysconf('SC_CLK_TCK')
    try:
        now = float((proc / 'uptime').read_text().split()[0]) * hertz
    except (OSError, ValueError, IndexError):
        now = 0
    rows = {}
    for path in proc.iterdir():
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError()
        if not path.name.isdigit():
            continue
        try:
            text = (path / 'stat').read_text()
            name, _, tail = text.partition('(')[2].rpartition(')')
            fields = tail.split()
            rows[int(path.name)] = dict(ppid=int(fields[1]), ticks=int(fields[11]) + int(fields[12]),
                                        start=int(fields[19]), mem=max(0, int(fields[21])) * os.sysconf('SC_PAGE_SIZE'),
                                        born=boot + int(fields[19]) / hertz if boot else None, daemon=name == 'pueued')
        except (OSError, ValueError, IndexError):  # procesos que terminaron durante la lectura
            continue
    return (sum(ticks), sum(ticks) - ticks[3] - ticks[4], now), rows


def process_sample(mode, cores, proc, deadline):
    if mode == 'macos':
        rows = {}
        now = time.time()
        raw = command(['/bin/ps', '-Aww', '-o', 'pid=,ppid=,%cpu=,rss=,etime=,args='], timeout=max(.01, deadline - time.monotonic()))
        for line in raw.splitlines():
            pid, ppid, cpu, rss, etime, argv = line.split(None, 5)
            rows[int(pid)] = dict(ppid=int(ppid), cpu=float(cpu) / 100, mem=int(rss) * 1024, born=now - elapsed(etime),
                                  argv=argv, daemon=bool(re.match(r'(?:\S*/)?pueued(?:\s|$)', argv)))
        return 0, sum(row['cpu'] for row in rows.values()), rows
    started = time.monotonic()
    first, before = linux_processes(proc, deadline)
    if deadline - time.monotonic() < 1:
        raise TimeoutError()
    time.sleep(1)
    last, rows = linux_processes(proc, deadline)
    interval = time.monotonic() - started
    total = last[0] - first[0]
    if total <= 0:
        raise ValueError('Contadores de CPU sin avance')
    for pid, row in rows.items():
        old = before.get(pid)
        if old and old['start'] == row['start']:
            used = max(0, row['ticks'] - old['ticks'])
        else:  # nació durante la muestra: todo su contador cae en el intervalo
            used = row['ticks'] if first[2] and row['start'] >= first[2] else 0
        row['cpu'] = used * cores / total
        # Sólo se necesita argv del daemon y de sus hijos directos.
        if row['daemon'] or rows.get(row['ppid'], {}).get('daemon'):
            try:
                row['argv'] = (proc / str(pid) / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            except OSError:
                row['argv'] = ''
    return interval, max(0, last[1] - first[1]) * cores / total, rows


def elapsed(text):
    """etime de ps: [[dd-]hh:]mm:ss."""
    days, _, clock = text.rpartition('-')
    seconds = 0
    for part in clock.split(':'):
        seconds = seconds * 60 + int(part)
    return seconds + int(days or 0) * 86400


def task_trees(rows, references, proc, mode, deadline=None):
    children = {}
    for pid, row in rows.items():
        children.setdefault(row['ppid'], []).append(pid)
    trees = {key: set() for key in references}
    for pid, row in rows.items():
        if not rows.get(row['ppid'], {}).get('daemon'):
            continue
        owners = [key for key, ref in references.items() if ref and
                  re.search(r'(?<![\w.-])' + re.escape(ref) + r'(?![\w.-])', row.get('argv', ''))]
        if len(owners) != 1:
            continue
        pending = [pid]
        tree = trees[owners[0]]
        while pending:
            child = pending.pop()
            if child not in tree:
                tree.add(child)
                pending.extend(children.get(child, []))
    contexts = {}
    for key, tree in trees.items():
        slots = set()
        if mode == 'linux':
            for pid in tree:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError()
                try:
                    cwd = os.readlink(str(proc / str(pid) / 'cwd'))
                except OSError:
                    continue
                match = re.search(r'(?:^|/)worktrees/sen-test-(\d+)(?:/|$)', cwd)
                if match:
                    slots.add(match[1])
        contexts[key] = dict(born={pid: rows[pid].get('born') for pid in tree}, slots=slots)
    return contexts


def pid_owner(container, ctx):
    """El PID del nombre es del árbol y ya existía al crear el contenedor (no es un PID reutilizado)."""
    match = re.search(r'-(\d+)$', container['name'])
    born = ctx['born'].get(int(match[1])) if match else None
    return born is not None and container['created'] is not None and born <= container['created'] + 1


# Una entrada por regla de atribución. Un contenedor con varios dueños queda fuera.
CONTAINER_RULES = (
    ('pid_suffix', pid_owner),
    ('jale_database', lambda container, ctx: any(container['name'] == 'sen-test-db-' + slot for slot in ctx['slots'])),
)


def docker_binary(mode):
    paths = ('/usr/local/bin/docker', '/opt/homebrew/bin/docker') if mode == 'macos' else ('/usr/bin/docker',)
    return next((path for path in paths if Path(path).is_file()), None)


def docker_bytes(text):
    match = re.fullmatch(r'([\d.]+)\s*(B|kB|KB|MB|GB|TB|KiB|MiB|GiB|TiB)', text.strip())
    if not match:
        raise ValueError('Memoria Docker inválida')
    unit = match[2]
    power = {'B': 0, 'k': 1, 'K': 1, 'M': 2, 'G': 3, 'T': 4}[unit[0]]
    return int(float(match[1]) * (1024 if 'i' in unit else 1000) ** power)


def docker_stats(mode, deadline):
    """Una sola lectura de todos los contenedores, en paralelo a la muestra de procesos."""
    binary = docker_binary(mode)
    if not binary:
        return 'absent', []
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        created = {}
        for line in command([binary, 'ps', '--format', '{{.Names}}\t{{.CreatedAt}}'], timeout=remaining).splitlines():
            name, _, stamp = line.partition('\t')
            try:
                created[name] = datetime.datetime.strptime(stamp[:25], '%Y-%m-%d %H:%M:%S %z').timestamp()
            except ValueError:
                created[name] = None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        raw = command([binary, 'stats', '--no-stream', '--format', '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'], timeout=remaining)
    except (OSError, subprocess.SubprocessError):
        return 'error', []
    rows = []
    for line in raw.splitlines():
        try:
            name, cpu, memory = line.split('\t')
            cpu, mem = float(cpu.rstrip('%')) / 100, docker_bytes(memory.split('/')[0])
        except (ValueError, OverflowError):  # un contenedor que arranca muestra «--»
            continue
        if math.isfinite(cpu) and cpu >= 0:
            rows.append(dict(name=name, created=created.get(name), cpu=cpu, mem=mem))
    return 'read', rows


def container_usage(stats, contexts):
    status, rows = stats
    usage = {}
    for container in rows:
        owners = [key for key, ctx in contexts.items() if any(rule(container, ctx) for _, rule in CONTAINER_RULES)]
        if len(owners) == 1:
            row = usage.setdefault(owners[0], dict(cpu=0., mem=0, containers=0))
            row['cpu'] += container['cpu']
            row['mem'] += container['mem']
            row['containers'] += 1
    return status, usage


def collect_usage(output, references, mode, proc):
    # docker stats tarda unos 2 s: corre mientras se muestrean los procesos.
    deadline = time.monotonic() + 4
    with ThreadPoolExecutor(1) as pool:
        stats = pool.submit(docker_stats, mode, deadline)
        interval, busy, rows = process_sample(mode, output['cpu']['cores'], proc, deadline)
        contexts = task_trees(rows, references, proc, mode, deadline)
        docker, containers = container_usage(stats.result(), contexts)
    for task in output['tasks']:
        tree = contexts.get(task['id'], {}).get('born')
        if task['state'] != 'Running' or not tree:
            continue
        extra = containers.get(task['id'], dict(cpu=0., mem=0, containers=0))
        task['usage'] = dict(cpu=sum(rows[pid]['cpu'] for pid in tree) + extra['cpu'],
                             mem=sum(rows[pid]['mem'] for pid in tree) + extra['mem'], containers=extra['containers'])
    attributed = [t['usage'] for t in output['tasks'] if t.get('usage')]
    used = max(0, int(output['mem']['total'] - output['mem']['available']))
    return dict(interval=interval, busy=busy, used=used, docker=docker,
                outside=dict(cpu=max(0, busy - sum(u['cpu'] for u in attributed)),
                             mem=max(0, used - sum(u['mem'] for u in attributed))))


def read_control(controller, host='mini'):
    answer = subprocess.run(['/usr/bin/python3', '-I', str(controller), 'rack', '--host', host],
        input=json.dumps(dict(schema='rack-host-control@1', op='capabilities')),
        check=True, text=True, capture_output=True, timeout=5)
    if len(answer.stdout) > (65536 if host=='mini' else 16384):
        raise ValueError('Respuesta excesiva')
    return json.loads(answer.stdout)


ORPHANS_LIMIT = 64
ORPHANS_BUDGET = 1.0


def orphan_stat(proc, pid):
    """Sólo stat: PPid, identidad, contadores y comm; nunca argv ni entorno."""
    raw = (proc / str(pid) / 'stat').read_text()
    comm, _, tail = raw.partition('(')[2].rpartition(')')
    fields = tail.split()
    return dict(ppid=int(fields[1]), ticks=int(fields[11]) + int(fields[12]),
                start=int(fields[19]), rss=max(0, int(fields[21])), comm=comm[:32])


def desk_orphans(data, proc=Path('/proc'), cgroup=Path('/sys/fs/cgroup'), unit='pueued.service'):
    """Lee una unidad; una sola espera compartida y plazo total de un segundo."""
    deadline = time.monotonic() + ORPHANS_BUDGET
    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError('Lectura de huérfanos agotada')
    main = int(command(['systemctl', '--user', 'show', unit, '-p', 'MainPID', '--value'], timeout=.5).strip())
    if main <= 1:
        raise ValueError('Servicio sin MainPID')
    leader = orphan_stat(proc, main)
    path = next(line.split(':', 2)[2] for line in (proc / str(main) / 'cgroup').read_text().splitlines()
                if line.startswith('0::'))
    if not path.startswith('/') or '..' in Path(path).parts or path == '/':
        raise ValueError('Cgroup inválido')
    members_file = cgroup / path.lstrip('/') / 'cgroup.procs'
    members = {int(pid) for pid in members_file.read_text().split()}
    if main not in members:
        raise ValueError('MainPID fuera del cgroup')
    boot = next(int(line.split()[1]) for line in (proc / 'stat').read_text().splitlines() if line.startswith('btime '))
    hertz, page = os.sysconf('SC_CLK_TCK'), os.sysconf('SC_PAGE_SIZE')
    def descendant(pid, row):
        seen = {pid}
        parent = row['ppid']
        while parent > 1:
            check()
            if parent == main:
                return True
            if parent in seen:
                raise ValueError('Cadena de padres cíclica')
            seen.add(parent)
            parent = orphan_stat(proc, parent)['ppid']
        return False
    candidates = {}
    for pid in members - {main}:
        check()
        try:
            row = orphan_stat(proc, pid)
            sampled = time.monotonic()  # junto a los contadores: la ascendencia no entra al intervalo
            if not descendant(pid, row):
                candidates[pid] = (row, sampled)
        except (FileNotFoundError, ProcessLookupError):
            continue
    if candidates:
        check()
        time.sleep(min(.05, max(0, deadline - time.monotonic())))
    rows = []
    # La segunda pertenencia evita publicar procesos que abandonaron la unidad.
    remaining = {int(pid) for pid in members_file.read_text().split()}
    for pid, (before, sampled) in candidates.items():
        check()
        if pid not in remaining:
            continue
        try:
            origin = None
            for fd in ('1', '2'):
                try:
                    target = os.readlink(proc / str(pid) / 'fd' / fd)
                except FileNotFoundError:
                    continue
                match = re.fullmatch(r'/.*/task_logs/([0-9]{1,20})\.log(?: \(deleted\))?', target)
                if match:
                    origin = match[1]
                    break
            after = orphan_stat(proc, pid)
            interval = time.monotonic() - sampled
            if before['start'] != after['start'] or descendant(pid, after):
                continue
        except (FileNotFoundError, ProcessLookupError):
            continue
        task = (data or {}).get('tasks', {}).get(origin, {}) if origin else {}
        status = task.get('status')
        state = status if isinstance(status, str) else next(iter(status or {}), None)
        details = status.get('Done', {}) if isinstance(status, dict) else {}
        state = 'Killed' if state == 'Done' and details.get('result') == 'Killed' else state
        group = task.get('group')
        rows.append(dict(pid=pid, comm=after['comm'], origin_task=origin,
                         group=group if isinstance(group, str) and len(group) <= 128 else None,
                         origin_state=state if state in ('Done', 'Killed', 'Running') else 'desconocido',
                         rss_bytes=after['rss'] * page,
                         cpu_percent=max(0, after['ticks'] - before['ticks']) / hertz / interval * 100
                         if 0 < interval <= .5 else None,
                         started_at=datetime.datetime.fromtimestamp(boot + after['start'] / hertz,
                             datetime.timezone.utc).isoformat().replace('+00:00', 'Z')))
    if orphan_stat(proc, main)['start'] != leader['start']:
        raise ValueError('MainPID cambió de identidad')
    check()
    rows.sort(key=lambda row: (-row['rss_bytes'], row['pid']))
    return rows[:ORPHANS_LIMIT], len(rows)


def within_budget(fn, seconds):
    """Corre fn en un hilo daemon y deja de esperarlo al vencer el plazo; muere con el proceso."""
    done, result = threading.Event(), []
    def run():
        try:
            result.append((True, fn()))
        except BaseException as error:
            result.append((False, error))
        finally:
            done.set()
    threading.Thread(target=run, daemon=True).start()
    if not done.wait(seconds):
        raise TimeoutError('Lectura de huérfanos agotada')
    ok, value = result[0]
    if not ok:
        raise value
    return value


def collect(mode, host, pueue=None, home=None, proc=Path('/proc'), now=None,
            cgroup=Path('/sys/fs/cgroup'), pueue_unit='pueued.service'):
    home, taken = home or Path.home(), time.time() if now is None else now
    output = dict(schema='rack-host@1', host=host, os=mode, taken=taken, cpu=None, mem=None, disk=None, guard=None, groups=[], tasks=[], errors=[])

    def attempt(stage, fn):
        try:
            return fn()
        except Exception as error:
            output['errors'].append(dict(stage=stage, message='Lectura fallida: ' + type(error).__name__))
            return None

    controller = home / ('.local/lib/pao-pueue-mini/control.py' if host == 'mini' else '.local/lib/pao-pueue-desk/control.py')
    if controller.is_file() and (host, mode) in (('mini', 'macos'), ('desk', 'linux')):
        output['control'] = attempt('control', lambda: read_control(controller) if host == 'mini' else read_control(controller, host))

    capacity = attempt('capacity', lambda: mac_capacity() if mode == 'macos' else linux_capacity(proc))
    if capacity:
        output['cpu'], output['mem'] = capacity
    if mode == 'linux' and output['cpu']:
        output['cpu']['psi_avg60'] = attempt('psi', lambda: float(re.search(r'^some .*avg60=([\d.]+)', (proc / 'pressure/cpu').read_text())[1]))
    output['disk'] = attempt('disk', lambda: disk('/System/Volumes/Data' if mode == 'macos' and Path('/System/Volumes/Data').exists() else '/'))
    output['guard'] = attempt('guard', lambda: mini_guard() if mode == 'macos' else desk_guard(home, proc, taken, output['errors']))
    admission = read_admission(home, time.time() if now is None else now) if (host, mode) == ('mini', 'macos') else []
    generation = admission_generation(home) if admission else None
    data = attempt('pueue', lambda: json.loads(command([pueue or ('/opt/homebrew/bin/pueue' if mode == 'macos' else str(home / '.local/bin/pueue')), 'status', '--json'])))
    same_generation = generation is not None and generation == admission_generation(home)
    if host == 'desk' and mode == 'linux':
        output['orphans'], output['orphans_total'] = attempt('orphans', lambda: within_budget(lambda: desk_orphans(data, proc, cgroup, pueue_unit), ORPHANS_BUDGET)) or ([], 0)
    interruptions = attempt('startup', lambda: desk_interruptions(home, host)) or {}
    queue_ages = (attempt('queue-age',lambda:mini_reorder_ages(home)) or {}) if host=='mini' else {}
    cache, references = {}, {}
    if data:
        def tasks():
            output['groups'] = [dict(name=name, status=g['status'], slots=g['parallel_tasks']) for name, g in data['groups'].items()]
            managed = match_admission(data['tasks'], admission, generation) if same_generation else {}
            for task in data['tasks'].values():
                state = task['status']
                state, details = (state, {}) if isinstance(state, str) else next(iter(state.items()))
                end = epoch(details.get('end'))
                result = details.get('result')
                if isinstance(result, dict):
                    result = 'exit ' + str(result['Failed']) if 'Failed' in result else next(iter(result))
                row = dict(id=task['id'], group=task['group'], state=state, enqueued=epoch(details.get('enqueued_at')), start=epoch(details.get('start')), end=end, result=result,
                           name=None, scope=None, project=None, ticket=None, repo=None, sha=None, branch=None, subject=None, priority=task.get('priority', 0))
                if state=='Queued' and queue_ages:
                    row['enqueued']=original_queue_age(task,details.get('enqueued_at'),queue_ages)
                interruption = interruptions.get(str(task['id']), {})
                attributed = (state == 'Done' and result == 'Killed' and interruption.get('created_at') == task.get('created_at')
                              and interruption.get('message') == 'Interrumpida al reiniciar Pueue; relanzar manualmente')
                if state == 'Done' and (end is None or end < taken - 86400) and not attributed:
                    continue
                admitted = managed.get(task['id'])
                jale = mode != 'macos' and re.fullmatch(r'exec bash (/.*/job-[^/\s]+/task\.sh)', task.get('command', ''))
                if admitted is not None:
                    metadata = dict(name=admitted['ref'])
                    row['admission'] = admitted
                else:
                    metadata = attempt('metadata', lambda: desk_metadata(task, home, cache) if jale else mini_metadata(task, home))
                row.update(metadata or {})
                if attributed:
                    row['interruption_reason'] = interruption['message']
                row['control'] = task_control(task, home, state)
                row['activity'] = task_activity(task, mode, home, metadata or {}, state in ('Running', 'Paused'),
                                                managed=admitted is not None)
                if state == 'Running':
                    ref = row['activity']['ref']
                    references[row['id']] = ref if jale else 'remote-run/runs/' + Path(task.get('path', '')).name
                output['tasks'].append(row)
        attempt('pueue', tasks)
    if references and output['cpu'] and output['mem']:
        output['usage'] = attempt('usage', lambda: collect_usage(output, references, mode, proc))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--os', choices=['macos', 'linux'], default='macos' if sys.platform == 'darwin' else 'linux')
    parser.add_argument('--host')
    args = parser.parse_args()
    print(json.dumps(collect(args.os, args.host or ('mini' if args.os == 'macos' else 'desk')), allow_nan=False))


if __name__ == '__main__':
    main()
