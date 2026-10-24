"""Tipos de confianza y proyecciones puras; ningún plazo libera reservas."""
import copy
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Tuple
from .protocol import demand, identifier, reason


@dataclass(frozen=True)
class AuthContext:
    principal: str
    role: str
    peer: object
    anchor: object
    channel_binding: object


@dataclass(frozen=True)
class NodeConfig:
    host_id: str
    store_id: str
    executors: Tuple[str, ...]
    budget: Mapping
    resources: Tuple[str, ...]
    state_dir: Path
    pending_ttl: float
    heartbeat_ttl: float
    revision: str
    acl: Mapping
    profile_minima: Mapping
    snapshot_host: str
    ncpu: int
    mode: str = 'shadow'
    executor_scope_kinds: Mapping = field(default_factory=dict)

    def __post_init__(self):
        for value in (self.host_id, self.store_id, self.revision, self.snapshot_host): identifier(value)
        demand(dict(self.budget))
        if self.mode != 'shadow' or not self.executors or type(self.ncpu) is not int or self.ncpu <= 0:
            raise ValueError('configuracion no acreditada')
        for ttl in (self.pending_ttl, self.heartbeat_ttl):
            if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0:
                raise ValueError('TTL del host invalido')
        path = self.state_dir
        if not isinstance(path, Path) or not path.is_absolute() or '..' in path.parts:
            raise ValueError('stateDir debe ser absoluto')
        if str(path).startswith('/mnt/c') or any(p in path.parts for p in ('.t3', '.git', 'fleet-pilot', 'pao', 'remote-run')):
            raise ValueError('stateDir reservado')
        for parent in (path,) + tuple(path.parents):
            if parent.is_symlink() or (parent / '.git').exists():
                raise ValueError('stateDir debe ser local y separado del checkout')
        if any(e not in self.executors or kind not in ('unix', 'windows')
               for e, kind in self.executor_scope_kinds.items()):
            raise ValueError('plataforma no acreditada')
        for minimum in self.profile_minima.values(): demand(dict(minimum))
        if len(set(self.resources)) != len(self.resources): raise ValueError('alias de recurso')


def identity_json(identity):
    if identity is None: return None
    return dict(executorId=identity.executor_id, uid=identity.uid, pid=identity.pid,
                startToken=list(identity.start_token), executorBoot=identity.executor_boot,
                physicalBoot=identity.physical_boot)


def expire_pending(now, view):
    """view contiene observaciones ya capturadas; no consulta reloj ni procesos."""
    transitions = []
    for p in view['pending']:
        if p['state'] != 'validating' or p.get('reservationKey') is not None:
            continue
        reasons = []
        if p['physicalBoot'] != view['physicalBoot'] or p['executorBoot'] != view['executorBoots'].get(p['executorId']):
            reasons.append(reason('boot-unverified', 'boot de preparacion cambio'))
        if now < p['created'] or now >= p['deadline'] or now - p['heartbeat'] >= view['heartbeatTTL']:
            reasons.append(reason('pending-expired', 'plazo o latido host vencido', True))
        for name, code in (('requesterObservation', 'requester-dead'), ('anchorObservation', 'anchor-dead')):
            status = p.get(name, 'unverifiable')
            if status == 'confirmed_absent_or_different': reasons.append(reason(code, name))
            if status == 'unverifiable': reasons.append(reason('identity-unverifiable', name))
        if reasons:
            transitions.append(dict(id=p['id'], state='expired' if any(r['code'] != 'identity-unverifiable' for r in reasons) else 'rejected', reasons=reasons))
        elif not p.get('channelAlive', False):
            transitions.append(dict(id=p['id'], state='withdrawn', reasons=[reason('requester-dead', 'EOF del canal de vida')]))
    return transitions


def effective(view, now, heartbeat_ttl):
    result = copy.deepcopy(view)
    updates = {t['id']: t for t in expire_pending(now, result)}
    for p in result['pending']:
        update = updates.get(p['id'])
        p['effectiveState'] = update['state'] if update else p['state']
        if update: p['reasons'] = update['reasons']
    for r in result['reservations']:
        reasons = list(r['reasons'])
        if r['state'] != 'released':
            if r['physicalBoot'] != result['physicalBoot'] or r['executorBoot'] != result['executorBoots'].get(r['executorId']):
                reasons.append(reason('boot-unverified', 'boot cambio; requiere reconciliacion'))
                result['drained'] = True
            if r.get('ownerObservation') != 'same':
                reasons.append(reason('identity-unverifiable', 'supervisor no verificable o ausente'))
            if not r.get('channelAlive', False) or now < r['heartbeat'] or now - r['heartbeat'] >= heartbeat_ttl:
                reasons.append(reason('reservation-uncertain', 'latido/control vencido o reloj retrocedio'))
        unique = { (x['code'], str(x['detail'])): x for x in reasons }
        r['reasons'] = list(unique.values())
        r['effectiveState'] = 'uncertain' if r['state'] != 'released' and reasons else r['state']
    return result


def budget_view(config, reservations):
    retained = dict(cpuMilli=0, memoryBytes=0, diskBytes={v: 0 for v in config.budget['diskBytes']}, slots=0)
    by_state = {}
    for r in reservations:
        state = r.get('effectiveState', r['state'])
        if state == 'released': continue
        by_state[state] = by_state.get(state, 0) + 1
        for dim in ('cpuMilli', 'memoryBytes', 'slots'): retained[dim] += r['demand'][dim]
        for volume, amount in r['demand']['diskBytes'].items():
            retained['diskBytes'][volume] = retained['diskBytes'].get(volume, 0) + amount
    available = {d: config.budget[d] - retained[d] for d in ('cpuMilli', 'memoryBytes', 'slots')}
    available['diskBytes'] = {v: config.budget['diskBytes'][v] - retained['diskBytes'].get(v, 0) for v in config.budget['diskBytes']}
    return dict(capacity=dict(config.budget), retained=retained, available=available, retainedByState=by_state)
