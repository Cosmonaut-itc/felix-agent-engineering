#!/usr/bin/env python3
"""Instantánea saneada del host; compatible con Python 3.9."""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import datetime
import json
import math
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
                times.append(path.stat().st_mtime)
            except OSError:
                continue
        activity['last'] = max(times) if times else None
    return activity


def task_control(task, home, state):
    """Identity from queue fields only. Capability remains the launcher's decision."""
    launcher, ref = None, None
    run = Path(task.get('path', '')).expanduser()
    if run.parent == home / 'remote-run/runs' and re.fullmatch(r'[0-9]{8}-[0-9]{6}-[0-9a-f]{6}', run.name):
        launcher, ref = 'remote-run', run.name
    else:
        match = re.fullmatch(r'exec bash (/.*/job-[A-Za-z0-9]{8}/task\.sh)', task.get('command', ''))
        job = Path(match[1]).parent if match else None
        if job and job.parent == home / '.local/state/jale/pueue':
            launcher, ref = 'jale', job.name
    reason = None
    if launcher is None:
        reason = 'Lanzador no reconocido; no se puede confirmar la cancelación.'
    elif state not in ('Queued', 'Running'):
        reason = 'Sólo se cancelan corridas en fila o en curso.'
    return dict(launcher=launcher, ref=ref,
                expected=dict(task_id=task['id'], state=state) if state in ('Queued', 'Running') else None,
                reason=reason)


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
    cache, references = {}, {}
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
                row['control'] = task_control(task, home, state)
                row['activity'] = task_activity(task, mode, home, metadata or {}, state in ('Running', 'Paused'))
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
