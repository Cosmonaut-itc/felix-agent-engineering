"""API de sombra en proceso: serialización, decisiones y transiciones simuladas."""
import copy
import json
import uuid
from dataclasses import asdict
from .identity import from_json
from .model import AuthContext, budget_view, effective, identity_json
from .protocol import SCHEMA, canonical, digest, reason, reply, validate
from .salud import lector_desk, lector_mini
from .scope import closure_reasons
from .store import Busy, ShadowStore, StoreError

THRESHOLDS = frozenset(('cpuRootPct', 'commitRatio', 'eLatencyMs', 'C: libre', 'E: libre',
                       'memLevelPct', 'memPressure', 'data libre'))


class Hostgate:
    def __init__(self, config, world):
        self.config, self.world = config, world
        self.store = ShadowStore(config)

    def _reply(self, op_id, reasons=(), would_grant=False, exit_code=0, **fields):
        return reply(op_id, self.config.revision, reasons, would_grant, exit_code, **fields)

    def _authorized(self, auth, operation):
        if not isinstance(auth, AuthContext): return False
        role = auth.role
        if role not in self.config.acl.get(auth.principal, ()) or auth.peer is None:
            return False
        allowed = ('supervisor', 'query', 'operator') if operation == 'estado' else ('supervisor', 'operator')
        return (role in allowed and self.world.verify(auth.peer).status == 'same'
                and (operation == 'estado' or self.world.channel_alive(auth.channel_binding)))

    @staticmethod
    def _auth_context(auth):
        return dict(principal=auth.principal, peer=identity_json(auth.peer),
                    anchor=identity_json(auth.anchor), channel=auth.channel_binding)

    def _observe(self, view):
        result = copy.deepcopy(view)
        result.update(physicalBoot=self.world.physical_boot(),
                      executorBoots={e: self.world.executor_boot(e) for e in self.config.executors},
                      heartbeatTTL=self.config.heartbeat_ttl)
        for pending in result['pending']:
            pending['requesterObservation'] = self.world.verify(from_json(pending['requester'])).status
            pending['anchorObservation'] = self.world.verify(from_json(pending['anchor'])).status if pending['anchor'] else 'unverifiable'
            pending['channelAlive'] = self.world.channel_alive(pending['channelBinding'])
        for reservation in result['reservations']:
            reservation['ownerObservation'] = self.world.verify(from_json(reservation['owner'])).status
            reservation['channelAlive'] = self.world.channel_alive(reservation['channelBinding'])
        return effective(result, self.world.monotonic(), self.config.heartbeat_ttl)

    def _persist_effective(self, view):
        projection = self._observe(view)
        for kind in ('pending', 'reservations'):
            for actual, projected in zip(view[kind], projection[kind]):
                actual['state'] = projected['effectiveState']
                actual['reasons'] = projected['reasons']
        view['drained'] = projection['drained']
        return projection

    def _health(self):
        kind, sample, independent_boot = self.world.health()
        reasons = []
        if kind == 'desk':
            if not self.world.default_shell:
                reasons.append(reason('default-shell-unverified', 'gate Desktop pendiente; transporte deshabilitado'))
            if not self.world.witness or independent_boot is None:
                reasons.append(reason('boot-unverified', 'SEN-3853 sin acreditar; WSL no es testigo Windows'))
                independent_boot = None
            raw = lector_desk.evaluar(sample, self.world.wall(), independent_boot)
        elif kind == 'mini':
            raw = lector_mini.evaluar(sample, self.world.wall(), independent_boot)
            if not isinstance(sample, dict) or sample.get('host') != self.config.snapshot_host:
                reasons.append(reason('health-invalid', 'host distinto de configuracion'))
            ncpu = sample.get('metrics', {}).get('ncpu', {}) if isinstance(sample, dict) and isinstance(sample.get('metrics'), dict) else {}
            if not isinstance(ncpu, dict) or ncpu.get('value') != self.config.ncpu:
                reasons.append(reason('executor-incompatible', 'ncpu difiere de configuracion'))
        else:
            return [reason('health-reader-pending', 'lector no disponible')]
        for detail in raw:
            reasons.append(reason('health-threshold' if detail in THRESHOLDS else 'health-invalid', detail))
        if independent_boot is None:
            reasons.append(reason('boot-unverified', 'boot independiente ausente'))
        return reasons

    def _identity_reasons(self, pending):
        reasons = []
        for field, code in (('requester', 'requester-dead'), ('anchor', 'anchor-dead')):
            identity = pending.get(field)
            observation = self.world.verify(from_json(identity)).status if identity else 'unverifiable'
            if observation != 'same':
                reasons.append(reason(code if observation == 'confirmed_absent_or_different' else 'identity-unverifiable', field))
        if not self.world.channel_alive(pending['channelBinding']):
            reasons.append(reason('requester-dead', 'canal de vida cerrado'))
        now = self.world.monotonic()
        if now < pending['created'] or now >= pending['deadline'] or now - pending['heartbeat'] >= self.config.heartbeat_ttl:
            reasons.append(reason('pending-expired', 'latido/plazo host vencido', True))
        if (pending['physicalBoot'] is None or pending['executorBoot'] is None
                or pending['physicalBoot'] != self.world.physical_boot()
                or pending['executorBoot'] != self.world.executor_boot(pending['executorId'])):
            reasons.append(reason('boot-unverified', 'identidad de arranque no vigente'))
        identity = pending['requester']
        if (identity['executorId'] != pending['executorId'] or identity['physicalBoot'] != pending['physicalBoot']
                or identity['executorBoot'] != pending['executorBoot']):
            reasons.append(reason('identity-unverifiable', 'ejecutor/boot de peer distinto'))
        return reasons

    def _new_pending(self, request, auth):
        now = self.world.monotonic()
        return dict(id='p_' + uuid.uuid4().hex, principal=auth.principal,
                    jobId=request['jobId'], attemptId=request['attemptId'], executorId=request['executorId'],
                    physicalHostId=request['physicalHostId'], operationId=request['operationId'],
                    requestDigest=digest(request), requester=identity_json(auth.peer), anchor=identity_json(auth.anchor),
                    channelBinding=auth.channel_binding, heartbeat=now, created=now,
                    deadline=now + self.config.pending_ttl, state='validating', reasons=[],
                    reservationKey=None, physicalBoot=self.world.physical_boot(),
                    executorBoot=self.world.executor_boot(request['executorId']))

    def prepare(self, request, auth):
        """Preparación interna del harness, sin reserva, turno ni endpoint CLI."""
        validate(request, 'reservar')
        if not self._authorized(auth, 'reservar'): raise PermissionError('no autorizado')
        op_id = request['operationId']
        def apply(view, original):
            self._persist_effective(view)
            if original:
                if original[2] != canonical(self._auth_context(auth)):
                    raise PermissionError('preparacion de otro dueno/canal')
                if original[0] != digest(request): raise ValueError('operation-conflict')
                return json.loads(original[1])
            self._persist_effective(view)
            pending = self._new_pending(request, auth)
            view['pending'].append(pending)
            return {'pendingId': pending['id']}
        result = self.store.mutate(auth.principal + '\x00preparation', op_id, digest(request), apply, self.world, self._auth_context(auth))
        return result['pendingId']

    def _decision(self, request, view, pending):
        profile_verified = self.world.profile_verified(request['profileId'])
        health_reasons = self._health()
        # La E/S previa no puede acreditar la vida que se valida para el commit.
        self._persist_effective(view)
        reasons = list(pending['reasons']) + health_reasons + self._identity_reasons(pending)
        if view['drained']: reasons.append(reason('drained', 'reapertura exige reconciliacion explicita'))
        if request['physicalHostId'] != self.config.host_id or request['executorId'] not in self.config.executors:
            reasons.append(reason('executor-incompatible', 'host/ejecutor no configurado'))
        if not profile_verified:
            reasons.append(reason('profile-unverified', 'perfil real sin acreditacion de no escape/delegacion'))
        held = [r for r in view['reservations'] if r['state'] != 'released']
        if any(r['state'] == 'uncertain' for r in held): reasons.append(reason('reservation-uncertain', 'host cerrado por reserva dudosa'))
        if any(r['jobId'] == request['jobId'] for r in held): reasons.append(reason('resource-busy', 'trabajo ya tiene reserva', True))
        incoming = copy.deepcopy(request['demand'])
        minimum = self.config.profile_minima.get(request['profileId'])
        if minimum is None:
            reasons.append(reason('profile-unverified', 'minimos efectivos no acreditados'))
        else:
            for dimension in ('cpuMilli', 'memoryBytes', 'slots'):
                incoming[dimension] = max(incoming[dimension], minimum[dimension])
            for volume, amount in minimum['diskBytes'].items():
                incoming['diskBytes'][volume] = max(incoming['diskBytes'].get(volume, 0), amount)
        budget = budget_view(self.config, view['reservations'])
        for dimension in ('cpuMilli', 'memoryBytes', 'slots'):
            if incoming[dimension] > budget['available'][dimension]:
                reasons.append(reason('budget-exceeded', dict(dimension=dimension, demand=incoming[dimension], available=budget['available'][dimension]), True))
        for volume, amount in incoming['diskBytes'].items():
            if volume not in self.config.budget['diskBytes'] or amount > budget['available']['diskBytes'][volume]:
                reasons.append(reason('budget-exceeded', dict(dimension='diskBytes:' + volume, demand=amount, available=budget['available']['diskBytes'].get(volume)), True))
        if any(r['code'] == 'budget-exceeded' for r in reasons): reasons.append(reason('waiting-capacity', 'Fleet conserva su cola; hostgate no espera', True))
        used = {name for r in held for name in r['exclusiveResources']}
        for resource in request['exclusiveResources']:
            if resource not in self.config.resources or resource in used:
                reasons.append(reason('resource-busy', resource, resource in used))
        return reasons, incoming

    def _run(self, request, auth, action):
        validate(request)
        op_id = request['operationId']
        if not self._authorized(auth, request['operation']):
            return self._reply(op_id, [reason('identity-unverifiable', 'endpoint/rol no autorizado')], exit_code=5)
        operation_digest = digest(request)
        def apply(view, original):
            projection = self._persist_effective(view)
            if original:
                if original[2] != canonical(self._auth_context(auth)):
                    return self._reply(op_id, [reason('identity-unverifiable', 'operacion de otro dueno/canal')], exit_code=5)
                if original[0] != operation_digest:
                    return self._reply(op_id, [reason('operation-conflict', 'operationId ya tiene otro payload')], exit_code=2)
                historic = json.loads(original[1])
                key = historic.get('simulationKey')
                current = next((r for r in projection['reservations'] if r['key'] == key), None)
                current_state = {'state': current['effectiveState'], 'reasons': current['reasons']} if current else None
                # El histórico es diagnóstico; un replay nunca vuelve a admitir.
                result = copy.deepcopy(historic)
                result.update(replayed=True, originalResult=historic, currentState=current_state, wouldGrant=False)
                return result
            return action(view)
        try:
            return self.store.mutate(auth.principal, op_id, operation_digest, apply, self.world, self._auth_context(auth))
        except Busy:
            return self._reply(op_id, [reason('busy', 'escritor ocupado', True)], exit_code=4)
        except StoreError:
            return self._reply(op_id, [reason('store-unavailable', 'estado ausente/corrupto/no acreditado')], exit_code=6)
        except OSError:
            return self._reply(op_id, [reason('store-unavailable', 'commit no confirmado; releer misma operacion')], exit_code=3)

    def reservar(self, request, auth):
        validate(request, 'reservar')
        def action(view):
            pending = next((p for p in view['pending'] if p['principal'] == auth.principal and p['operationId'] == request['operationId']), None)
            if pending is not None and (pending['requester'] != identity_json(auth.peer)
                                        or pending['anchor'] != identity_json(auth.anchor)
                                        or pending['channelBinding'] != auth.channel_binding):
                return self._reply(request['operationId'], [reason('identity-unverifiable', 'preparacion de otro dueno/canal')], exit_code=5)
            if pending is not None and pending['requestDigest'] != digest(request):
                return self._reply(request['operationId'], [reason('operation-conflict', 'preparacion de otro payload')], exit_code=2)
            if pending is None:
                pending = self._new_pending(request, auth)
                orphan = next((p for p in view['pending'] if p['principal'] == auth.principal
                               and p['requester'] == pending['requester'] and p['jobId'] == pending['jobId']
                               and p['attemptId'] == pending['attemptId'] and p['state'] in ('expired', 'withdrawn')), None)
                if orphan:
                    pending.update(state='expired', reasons=[reason('pending-expired', 'lapida de solicitante; nueva operacion no revive al huerfano')])
                view['pending'].append(pending)
            self.world.checkpoint('before_revalidate')
            reasons, incoming = self._decision(request, view, pending)
            if pending['state'] != 'validating' or reasons:
                if pending['state'] == 'validating': pending['state'] = 'rejected'
                pending['reasons'] = reasons or [reason('pending-expired', 'preparacion terminal')]
                return self._reply(request['operationId'], pending['reasons'], exit_code=1, pendingId=pending['id'])
            generation = view['generations'].get(request['jobId'], 0) + 1
            view['generations'][request['jobId']] = generation
            key = dict(storeId=self.config.store_id, reservationId='s_' + uuid.uuid4().hex, generation=generation)
            reservation = dict(key=key, jobId=request['jobId'], attemptId=request['attemptId'],
                               executorId=request['executorId'], physicalHostId=request['physicalHostId'],
                               principal=auth.principal, owner=identity_json(auth.peer),
                               channelBinding=auth.channel_binding, demand=incoming,
                               exclusiveResources=list(request['exclusiveResources']), profileId=request['profileId'],
                               scopeKind=self.config.executor_scope_kinds.get(request['executorId'], 'unix'), scope=key['reservationId'], launchPossible=False,
                               state='reserved', heartbeat=self.world.monotonic(), seq=0, reasons=[],
                               physicalBoot=self.world.physical_boot(), executorBoot=self.world.executor_boot(request['executorId']))
            view['reservations'].append(reservation)
            pending.update(state='granted', reservationKey=key)
            return self._reply(request['operationId'], would_grant=True, simulationKey=key, pendingId=pending['id'], simulationState='reserved')
        return self._run(request, auth, action)

    def _owner_mutation(self, op_id, key, operation, auth, extras, mutate):
        request = dict(schema=SCHEMA, operationId=op_id, operation=operation, key=key, **extras)
        def action(view):
            reservation = next((r for r in view['reservations'] if r['key'] == key), None)
            if reservation is None or key['storeId'] != self.config.store_id:
                return self._reply(op_id, [reason('stale-generation', 'storeId/reservationId/generation no vigente')], exit_code=1)
            if reservation['principal'] != auth.principal or reservation['owner'] != identity_json(auth.peer):
                return self._reply(op_id, [reason('identity-unverifiable', 'no es el dueno exacto')], exit_code=5)
            if (self.world.verify(auth.peer).status != 'same' or not self.world.channel_alive(auth.channel_binding)
                    or reservation['channelBinding'] != auth.channel_binding):
                return self._reply(op_id, [reason('identity-unverifiable', 'dueno/canal no verificable')], exit_code=5)
            reasons = mutate(reservation)
            return self._reply(op_id, reasons, exit_code=1 if reasons else 0, simulationKey=key,
                               simulationState=reservation['state'])
        return self._run(request, auth, action)

    def latido(self, op_id, key, seq, auth):
        def mutate(reservation):
            if reservation['state'] == 'released': return []
            if seq <= reservation['seq']: return [reason('operation-conflict', 'secuencia no creciente')]
            reservation.update(seq=seq, heartbeat=self.world.monotonic())
            return []
        return self._owner_mutation(op_id, key, 'latido', auth, dict(seq=seq), mutate)

    def dudosa(self, op_id, key, reason_text, evidence, auth):
        def mutate(reservation):
            if reservation['state'] != 'released':
                reservation['state'] = 'uncertain'
                reservation['reasons'].append(reason('reservation-uncertain', dict(reason=reason_text, evidenceIds=list(evidence))))
            return []
        return self._owner_mutation(op_id, key, 'dudosa', auth, dict(reason=reason_text, evidenceIds=list(evidence)), mutate)

    def soltar(self, op_id, key, evidence, auth):
        def mutate(reservation):
            if reservation['state'] == 'released': return []
            closure = self.world.closure(evidence, reservation)
            reasons = closure_reasons(closure, reservation)
            if reasons:
                reservation['state'] = 'uncertain'
                reservation['reasons'].extend(reasons)
            else:
                reservation.update(state='released', reasons=[], closureEvidence=asdict(closure))
            return reasons
        return self._owner_mutation(op_id, key, 'soltar', auth, dict(evidenceIds=list(evidence)), mutate)

    def estado(self, query, auth):
        if query: raise ValueError('consulta desconocida')
        if not self._authorized(auth, 'estado'):
            return self._reply(None, [reason('identity-unverifiable', 'consulta no autorizada')], exit_code=5)
        view = self._observe(self.store.read())
        for kind in ('pending', 'reservations'):
            for item in view[kind]:
                item.pop('channelBinding', None)
                if 'heartbeat' in item: item['heartbeatAgeSeconds'] = self.world.monotonic() - item['heartbeat']
        resources = [dict(name=name, holder=r['key'], state=r['effectiveState'])
                     for r in view['reservations'] if r['effectiveState'] != 'released'
                     for name in r['exclusiveResources']]
        health_reasons = self._health()
        return self._reply(None, health_reasons, pending=view['pending'], reservations=view['reservations'],
                           budget=budget_view(self.config, view['reservations']), resources=resources,
                           drained=view['drained'], degraded=bool(health_reasons),
                           physicalHostId=self.config.host_id)
