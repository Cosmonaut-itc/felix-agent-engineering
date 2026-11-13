#!/usr/bin/env python3
"""Instantánea saneada del host; compatible con Python 3.9."""
import argparse
import ast
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


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
                project=match[1] if match else None, ticket=ticket((name or '') + ' ' + src), branch=branch, subject=subject)


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


def task_activity(task, mode, home, metadata, active=True):
    """Sólo stat de salidas conocidas; nunca se abre un log."""
    activity = dict(metadata.get('activity') or dict(last=None, kind=None, ref=None))
    jale = re.fullmatch(r'exec bash (/.*/job-[^/\s]+/task\.sh)', task.get('command', ''))
    run = Path(task.get('path', '')).expanduser()
    logs = [home / ('Library/Application Support/pueue' if mode == 'macos' else '.local/share/pueue')
            / 'task_logs' / (str(task['id']) + '.log')]
    if jale:
        job = Path(jale[1]).parent
        activity['ref'] = job.name
        activity['kind'] = activity['kind'] or 'chain'
        logs.append(job / 'log')
    elif run.parent == home / 'remote-run/runs':
        activity['ref'] = run.name
        logs.append(run / 'log')
    if active:
        times = []
        for path in logs:
            try:
                stat = path.stat()
                # El tamaño se consulta junto con mtime, sin leer contenido.
                if stat.st_size >= 0:
                    times.append(stat.st_mtime)
            except OSError:
                continue
        activity['last'] = max(times) if times else None
    return activity


def desk_guard(home):
    thresholds = {'pause': {'cpu': 30}, 'resume': {'cpu': 15}}
    source = home / '.local/lib/pao-pueue-desk/control.py'
    if source.exists():
        for node in ast.parse(source.read_text()).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'PRESSURE_THRESHOLDS' for t in node.targets):
                # El archivo usa multiplicaciones; sólo se necesitan los literales CPU.
                thresholds = {k.value: {'cpu': next(v.value for a, v in zip(d.keys, d.values) if a.value == 'cpu')}
                              for k, d in zip(node.value.keys, node.value.values)}
    last = None
    path = home / '.local/state/pao/pueue-desk/guardia.log'
    if path.exists():
        with path.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if row.get('evento') == 'decision':
                        last = row
                except ValueError:
                    continue
    if last and last.get('accion') in ('pausar', 'reanudar'):
        return dict(action=last['accion'], at=last['utc'], cpu_pause=thresholds['pause']['cpu'], cpu_resume=thresholds['resume']['cpu'])
    return None


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


def collect(mode, host, pueue=None, home=None, proc=Path('/proc'), now=None):
    home, now = home or Path.home(), time.time() if now is None else now
    output = dict(schema='rack-host@1', host=host, os=mode, taken=now, cpu=None, mem=None, disk=None, guard=None, groups=[], tasks=[], errors=[])

    def attempt(stage, fn):
        try:
            return fn()
        except Exception as error:
            output['errors'].append(dict(stage=stage, message='Lectura fallida: ' + type(error).__name__))
            return None

    capacity = attempt('capacity', lambda: mac_capacity() if mode == 'macos' else linux_capacity(proc))
    if capacity:
        output['cpu'], output['mem'] = capacity
    if mode == 'linux' and output['cpu']:
        output['cpu']['psi_avg60'] = attempt('psi', lambda: float(re.search(r'^some .*avg60=([\d.]+)', (proc / 'pressure/cpu').read_text())[1]))
    output['disk'] = attempt('disk', lambda: disk('/System/Volumes/Data' if mode == 'macos' and Path('/System/Volumes/Data').exists() else '/'))
    output['guard'] = attempt('guard', lambda: mini_guard() if mode == 'macos' else desk_guard(home))
    data = attempt('pueue', lambda: json.loads(command([pueue or ('/opt/homebrew/bin/pueue' if mode == 'macos' else str(home / '.local/bin/pueue')), 'status', '--json'])))
    cache = {}
    if data:
        def tasks():
            output['groups'] = [dict(name=name, status=g['status'], slots=g['parallel_tasks']) for name, g in data['groups'].items()]
            for task in data['tasks'].values():
                state = task['status']
                state, details = (state, {}) if isinstance(state, str) else next(iter(state.items()))
                end = epoch(details.get('end'))
                if state == 'Done' and (end is None or end < now - 86400):
                    continue
                result = details.get('result')
                if isinstance(result, dict):
                    result = 'exit ' + str(result['Failed']) if 'Failed' in result else next(iter(result))
                row = dict(id=task['id'], group=task['group'], state=state, enqueued=epoch(details.get('enqueued_at')), start=epoch(details.get('start')), end=end, result=result,
                           name=None, scope=None, project=None, ticket=None, repo=None, sha=None, branch=None, subject=None, priority=task.get('priority', 0))
                jale = mode != 'macos' and re.fullmatch(r'exec bash (/.*/job-[^/\s]+/task\.sh)', task.get('command', ''))
                metadata = attempt('metadata', lambda: desk_metadata(task, home, cache) if jale else mini_metadata(task, home))
                row.update(metadata or {})
                row['activity'] = task_activity(task, mode, home, metadata or {}, state in ('Running', 'Paused'))
                output['tasks'].append(row)
        attempt('pueue', tasks)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--os', choices=['macos', 'linux'], default='macos' if sys.platform == 'darwin' else 'linux')
    parser.add_argument('--host')
    args = parser.parse_args()
    print(json.dumps(collect(args.os, args.host or ('mini' if args.os == 'macos' else 'desk')), allow_nan=False))


if __name__ == '__main__':
    main()
