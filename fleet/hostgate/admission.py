"""API en proceso: serialización, decisiones y transiciones de sombra y de la autoridad efectiva."""
import copy
import json
import uuid
from .identity import from_json
from .model import AuthContext, budget_view, effective, identity_json
from .protocol import SCHEMA, canonical, digest, reason, reply, validate
from .salud import lector_desk, lector_mini
from . import scope
from .scope import Refused, closure_reasons, creation_from_row, evidence_json, permit_view
from .store import Busy, ForeignOwner, SerializedMutation, store_for, StoreError, UnrecordedResult, WriteUncertain

THRESHOLDS = frozenset(('cpuRootPct', 'commitRatio', 'eLatencyMs', 'C: libre', 'E: libre',
                       'memLevelPct', 'memPressure', 'data libre'))


class Hostgate:
    def __init__(self, config, world):
        self.config, self.world = config, world
        self.store = store_for(config)
        self._epoch = None  # Epoch del emisor de este proceso; se fija en su primera operación de permiso.

    def _reply(self, op_id, reasons=(), would_grant=False, exit_code=0, **fields):
        return reply(op_id, self.config.revision, reasons, would_grant, exit_code, mode=self.config.mode,
                     store_id=self.config.store_id, **fields)

    def _foreign_owner(self, op_id):
        return ForeignOwner(self._reply(
            op_id, [reason('identity-unverifiable', 'preparacion/reserva de otro dueno/canal')], exit_code=5))

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
        result['drained'] = result['drained'] or self.config.drained
        result.update(physicalBoot=self.world.physical_boot(),
                      executorBoots={e: self.world.executor_boot(e) for e in self.config.executors},
                      heartbeatTTL=self.config.heartbeat_ttl)
        for pending in result['pending']:
            if pending['state'] != 'validating': continue
            pending['requesterObservation'] = self.world.verify(from_json(pending['requester'])).status
            pending['anchorObservation'] = self.world.verify(from_json(pending['anchor'])).status if pending['anchor'] else 'unverifiable'
            pending['channelAlive'] = self.world.channel_alive(pending['channelBinding'], pending=True)
        for reservation in result['reservations']:
            if reservation['state'] == 'released': continue
            reservation['ownerObservation'] = self.world.verify(from_json(reservation['owner'])).status
            reservation['channelAlive'] = self.world.channel_alive(reservation['channelBinding'])
        return effective(result, self.world.monotonic(), self.config.heartbeat_ttl)

    @staticmethod
    def _copy_projection(view, projection):
        for kind in ('pending', 'reservations'):
            identity = (lambda row: row['id']) if kind == 'pending' else (lambda row: canonical(row['key']))
            observed = {identity(row): row for row in projection[kind]}
            for saved in view[kind]:
                projected = observed.get(identity(saved))
                if projected is not None:
                    saved.update(state=projected['effectiveState'], reasons=copy.deepcopy(projected['reasons']))
        view['drained'] = view['drained'] or projection['drained']

    def _persist_effective(self, view):
        projection = self._observe(view)
        self._copy_projection(view, projection)
        return projection

    def _guarded_apply(self, op_id, view, original, apply, context, failures=None):
        conservative = copy.deepcopy(view)

        def project(candidate):
            projection = self._persist_effective(candidate)
            # La acción puede reordenar filas o añadir otras que no deben sobrevivir al fallo.
            self._copy_projection(conservative, projection)
            return projection

        try:
            try:
                result = apply(view, original, project)
            except ForeignOwner as exc:
                result = exc
            # Serializar exactamente lo que escribirá el store, incluidos rechazos
            # sin operación y su audit. Fuera de esta guarda sólo queda la E/S.
            return SerializedMutation.build(view, result, context, original)
        except Exception as exc:
            # El fallo conserva observaciones, sin confirmar cambios parciales de la acción.
            view.clear()
            view.update(conservative)
            if failures is not None:
                failures.append(exc)
            result = UnrecordedResult(self._reply(
                op_id, [reason('store-unavailable', 'proveedor interno no disponible')], exit_code=6))
            return SerializedMutation.build(view, result, context, original)

    def _health(self):
        try:
            return self._read_health()
        except Exception:
            return [reason('health-reader-pending', 'proveedor/lector de salud no disponible')]

    def _read_health(self):
        kind, sample, independent_boot = self.world.health()
        reasons = []
        if kind == 'desk':
            if not self.world.default_shell:
                reasons.append(reason('default-shell-unverified', 'gate Desktop pendiente; transporte deshabilitado'))
            if not self.world.witness or independent_boot is None:
                reasons.append(reason('boot-unverified', 'SEN-3853 sin acreditar; WSL no es testigo Windows'))
                independent_boot = None
            try:
                raw = lector_desk.evaluar(sample, self.world.wall(), independent_boot)
            except OverflowError:
                # El lector aceptado pertenece a Desktop; el adaptador cierra su desborde.
                raw = ['metrica fuera de rango numerico']
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
        if not self.world.channel_alive(pending['channelBinding'], pending=True):
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
        if not self._authorized(auth, 'reservar'): raise self._foreign_owner(request['operationId'])
        op_id = request['operationId']
        failures = []
        context = self._auth_context(auth)
        def apply(view, original, project):
            project(view)
            if original:
                if original[2] != canonical(context):
                    raise self._foreign_owner(op_id)
                if original[0] != digest(request):
                    failures.append(ValueError('operation-conflict'))
                    return UnrecordedResult(self._reply(
                        op_id, [reason('operation-conflict', 'operationId ya tiene otro payload')], exit_code=2))
                return json.loads(original[1])
            pending = self._new_pending(request, auth)
            view['pending'].append(pending)
            return {'pendingId': pending['id']}
        def guarded_apply(view, original):
            return self._guarded_apply(op_id, view, original, apply, context, failures)
        result = self.store.mutate(auth.principal + '\x00preparation', op_id, digest(request), guarded_apply, self.world, context)
        # El harness espera la excepción original, pero sólo tras confirmar la observación.
        if failures:
            raise failures[0]
        return result['pendingId']

    def _decision(self, request, view, pending, project):
        profile_verified = self.world.profile_verified(request['profileId'])
        health_reasons = self._health()
        # La E/S previa no puede acreditar la vida que se valida para el commit.
        project(view)
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
        operation_digest = digest(request)
        def apply(view, original, project):
            projection = project(view)
            if original:
                if original[2] != canonical(context):
                    raise self._foreign_owner(op_id)
                if original[0] != operation_digest:
                    return self._reply(op_id, [reason('operation-conflict', 'operationId ya tiene otro payload')], exit_code=2)
                historic = json.loads(original[1])
                key = historic.get('simulationKey' if self.config.mode == 'shadow' else 'reservationKey')
                current = next((r for r in projection['reservations'] if r['key'] == key), None)
                current_state = {'state': current['effectiveState'], 'reasons': current['reasons']} if current else None
                if current and 'creation' in current: current_state['permit'] = permit_view(creation_from_row(current))
                # El histórico es diagnóstico; un replay nunca vuelve a admitir ni a habilitar una creación.
                result = copy.deepcopy(historic)
                result.update(replayed=True, originalResult=historic, currentState=current_state)
                result['wouldGrant' if self.config.mode == 'shadow' else 'granted'] = False
                if 'creationAllowed' in result: result['creationAllowed'] = False
                return result
            pending = next((p for p in view['pending']
                            if p['principal'] == auth.principal and p['operationId'] == op_id), None)
            if pending is not None:
                if (pending['requester'] != identity_json(auth.peer)
                        or pending['anchor'] != identity_json(auth.anchor)
                        or pending['channelBinding'] != auth.channel_binding):
                    raise self._foreign_owner(op_id)
                if request['operation'] != 'reservar':
                    return UnrecordedResult(self._reply(
                        op_id, [reason('operation-conflict', 'operationId reservado por preparacion')], exit_code=2))
            return action(view, project)
        def guarded_apply(view, original):
            return self._guarded_apply(op_id, view, original, apply, context)
        try:
            if not self._authorized(auth, request['operation']):
                return self._reply(op_id, [reason('identity-unverifiable', 'endpoint/rol no autorizado')], exit_code=5)
            context = self._auth_context(auth)
        except Exception:
            return self._reply(op_id, [reason('store-unavailable', 'proveedor de identidad no disponible')], exit_code=6)
        try:
            return self.store.mutate(auth.principal, op_id, operation_digest, guarded_apply, self.world, context)
        except ForeignOwner as exc:
            return exc.result
        except Busy:
            return self._reply(op_id, [reason('busy', 'escritor ocupado', True)], exit_code=4)
        except StoreError:
            return self._reply(op_id, [reason('store-unavailable', 'estado ausente/corrupto/no acreditado')], exit_code=6)
        except (WriteUncertain, OSError):
            return self._reply(op_id, [reason('store-unavailable', 'commit no confirmado; releer misma operacion')], exit_code=3)
        except Exception:
            return self._reply(op_id, [reason('store-unavailable', 'proveedor interno no disponible')], exit_code=6)

    def reservar(self, request, auth):
        validate(request, 'reservar')
        def action(view, project):
            pending = next((p for p in view['pending'] if p['principal'] == auth.principal and p['operationId'] == request['operationId']), None)
            if pending is not None and (pending['requester'] != identity_json(auth.peer)
                                        or pending['anchor'] != identity_json(auth.anchor)
                                        or pending['channelBinding'] != auth.channel_binding):
                raise self._foreign_owner(request['operationId'])
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
            reasons, incoming = self._decision(request, view, pending, project)
            if pending['state'] != 'validating' or reasons:
                if pending['state'] == 'validating': pending['state'] = 'rejected'
                pending['reasons'] = reasons or [reason('pending-expired', 'preparacion terminal')]
                return self._reply(request['operationId'], pending['reasons'], exit_code=1, pendingId=pending['id'])
            generation = view['generations'].get(request['jobId'], 0) + 1
            view['generations'][request['jobId']] = generation
            key = dict(storeId=self.config.store_id, reservationId=('s_' if self.config.mode == 'shadow' else 'e_') + uuid.uuid4().hex, generation=generation)
            reservation = dict(key=key, jobId=request['jobId'], attemptId=request['attemptId'],
                               executorId=request['executorId'], physicalHostId=request['physicalHostId'],
                               principal=auth.principal, owner=identity_json(auth.peer),
                               channelBinding=auth.channel_binding, demand=incoming,
                               exclusiveResources=list(request['exclusiveResources']), profileId=request['profileId'],
                               scopeKind=self.config.executor_scope_kinds.get(request['executorId'], 'unix'), scope=key['reservationId'], launchPossible=False,
                               state='reserved', heartbeat=self.world.monotonic(), seq=0, reasons=[],
                               physicalBoot=pending['physicalBoot'], executorBoot=pending['executorBoot'])
            view['reservations'].append(reservation)
            pending.update(state='granted', reservationKey=key)
            if self.config.mode == 'shadow':
                return self._reply(request['operationId'], would_grant=True, simulationKey=key,
                                   pendingId=pending['id'], simulationState='reserved')
            return self._reply(request['operationId'], would_grant=True, pendingId=pending['id'],
                               **self._key_fields(key, 'reserved'))
        return self._run(request, auth, action)

    def _key_fields(self, key, state, row=None):
        if self.config.mode == 'shadow': return dict(simulationKey=key, simulationState=state)
        fields = dict(reservationKey=key, state=state)
        if row is not None and 'creation' in row: fields['permit'] = permit_view(creation_from_row(row))
        return fields

    def _owner_mutation(self, op_id, key, operation, auth, extras, mutate, defaults=None):
        """Dueño y canal antes de leer el permiso. mutate da motivos o (motivos, campos, exitCode)."""
        request = dict(schema=SCHEMA, operationId=op_id, operation=operation, key=key, **extras)
        def action(view, project):
            reservation = next((r for r in view['reservations'] if r['key'] == key), None)
            if (reservation is None or key['storeId'] != self.config.store_id
                    or (self.config.mode == 'effective' and not key['reservationId'].startswith('e_'))):
                return self._reply(op_id, [reason('stale-generation', 'storeId/reservationId/generation no vigente')], exit_code=1)
            if reservation['principal'] != auth.principal or reservation['owner'] != identity_json(auth.peer):
                raise self._foreign_owner(op_id)
            if (self.world.verify(auth.peer).status != 'same' or not self.world.channel_alive(auth.channel_binding)
                    or reservation['channelBinding'] != auth.channel_binding):
                raise self._foreign_owner(op_id)
            try:
                outcome = mutate(reservation, view)
            except Refused as refused:
                if refused.doubt: self._doubt(reservation, refused.reason['detail'])
                outcome = [refused.reason], {}, 2 if refused.reason['code'] == 'operation-conflict' else 1
            if isinstance(outcome, list): outcome = outcome, {}, 1 if outcome else 0
            reasons, fields, exit_code = outcome
            return self._reply(op_id, reasons, exit_code=exit_code, **dict(defaults or {}, **fields),
                               **self._key_fields(key, reservation['state'], reservation))
        return self._run(request, auth, action)

    @staticmethod
    def _doubt(row, detail):
        """Duda durable: la reserva queda uncertain con su débito íntegro."""
        row['state'] = 'uncertain'
        doubt = reason('reservation-uncertain', detail)
        if doubt not in row['reasons']: row['reasons'].append(doubt)
        return doubt

    def _issuer_epoch(self, view):
        if self._epoch is None: self._epoch = scope.next_epoch(view)
        return self._epoch

    def _fence(self, row, view, auth, operation):
        """Fencing de la autoridad tras dueño y canal; devuelve el veredicto del perfil o lanza Refused."""
        if operation != 'soltar' and row['key']['generation'] != view['generations'].get(row['jobId']):
            raise Refused(reason('stale-generation', 'autorizar, consumir y registrar exigen la generacion vigente'))
        if row['state'] == 'released': return None
        if row['physicalBoot'] != self.world.physical_boot():
            raise Refused(reason('boot-unverified', 'boot del host distinto del de la reserva'))
        permit = getattr(creation_from_row(row), 'permit', None)
        if permit is not None and permit.issuer_epoch != self._issuer_epoch(view):
            # Muerte de hostgate: una creación nunca sigue con otro epoch.
            raise Refused(reason('reservation-uncertain', 'permiso de otro epoch del emisor'), doubt=True)
        if row['state'] == 'uncertain':
            raise Refused(reason('reservation-uncertain', 'reserva dudosa: no se crea ni se cierra'))
        if permit is None and operation != 'authorize_creation': return None
        verdict = self.world.profile_verified(row['profileId'], workload_uid=auth.peer.uid)
        revision = getattr(verdict, 'revision', None)
        if revision is None or (permit is not None and revision != permit.profile_revision):
            raise Refused(reason('profile-unverified', 'perfil sin acreditar o de otra revision'), doubt=permit is not None)
        return verdict

    def _creation(self, op_id, key, operation, auth, extras, mutate, defaults=None):
        if self.config.mode != 'effective':
            validate(dict(schema=SCHEMA, operationId=op_id, operation=operation, key=key, **extras))
            return self._reply(op_id, [reason('enforcement-unavailable', 'autoridad de creacion solo en modo efectivo')],
                               exit_code=5)
        return self._owner_mutation(op_id, key, operation, auth, extras, mutate, defaults)

    @staticmethod
    def _replace(row, moved):
        row.clear(); row.update(moved[0])

    def authorize_creation(self, op_id, key, confirmation, launch, auth):
        """unissued -> issued: la línea base se toma una sola vez, aquí, del proveedor de coalición."""
        def mutate(row, view):
            verdict = self._fence(row, view, auth, 'authorize_creation')
            grant = next((p for p in view['pending'] if p['reservationKey'] == row['key']), None)
            if (grant is None or grant['state'] != 'granted' or grant['operationId'] != confirmation['operationId']
                    or grant['requestDigest'] != confirmation['requestDigest']
                    or row['attemptId'] != confirmation['attemptId']):
                raise Refused(reason('operation-conflict', 'confirmacion distinta del grant original'))
            if type(creation_from_row(row)) is not scope.Unissued:
                raise Refused(reason('operation-conflict', 'la reserva ya tiene permiso; nunca se emite otro'))
            from .darwin_coalition import CoalitionUnavailable, Retry
            try:
                baseline = self.world.coalition.certify(scope.owner_birth(row))
            except CoalitionUnavailable as exc:
                failed = exc.observation
                return [reason('unix-scope-unconfirmed', 'linea base no certificada: ' + failed.reason,
                               isinstance(failed, Retry))], {}, 1
            self._replace(row, scope.authorize_creation(
                row, permit_id='pmt_' + uuid.uuid4().hex, confirmation_digest=digest(confirmation),
                launch_digest=digest(launch), baseline=baseline, issuer_epoch=self._issuer_epoch(view),
                profile_revision=verdict.revision))
            return [], dict(heartbeatTTL=self.config.heartbeat_ttl), 0
        extras = dict(confirmation=confirmation, launch=launch)
        return self._creation(op_id, key, 'authorize_creation', auth, extras, mutate)

    def consume_creation(self, op_id, key, permit_id, launch_digest, auth):
        """CAS issued -> consumed. Sólo este resultado original lleva creationAllowed verdadero."""
        def mutate(row, view):
            self._fence(row, view, auth, 'consume_creation')
            self._replace(row, scope.consume_creation(row, permit_id=permit_id, launch_digest=launch_digest))
            return [], dict(creationAllowed=True), 0
        extras = dict(permitId=permit_id, launchDigest=launch_digest)
        return self._creation(op_id, key, 'consume_creation', auth, extras, mutate, dict(creationAllowed=False))

    def record_started(self, op_id, key, permit_id, root, auth):
        """consumed -> registered con la raíz que hostgate observa; la reserva pasa a running."""
        def mutate(row, view):
            self._fence(row, view, auth, 'record_started')
            creation = creation_from_row(row)
            if type(creation) is scope.Registered:
                same = [creation.root.birth.pid, creation.root.birth.unique] == [root['pid'], root['unique']]
                raise Refused(reason('operation-conflict', 'raiz ya registrada' if same else 'otra raiz tras el registro'),
                              doubt=not same)
            from .darwin_coalition import RootRejected
            owner = scope.owner_birth(row)
            try:
                observed = self.world.coalition.observe_root(owner, root['pid']) if type(creation) is scope.Consumed else None
            except RootRejected as exc:
                raise Refused(reason('identity-unverifiable', 'raiz rechazada: %s' % exc))
            if observed is not None and observed.birth.unique != root['unique']:
                raise Refused(reason('identity-unverifiable', 'uniqueid de la raiz distinto del observado'))
            self._replace(row, scope.record_started(row, permit_id=permit_id, root=observed))
            return []
        return self._creation(op_id, key, 'record_started', auth, dict(permitId=permit_id, root=dict(root)), mutate)

    def latido(self, op_id, key, seq, auth):
        def mutate(reservation, view):
            if reservation['state'] == 'released': return []
            if seq <= reservation['seq']: return [reason('operation-conflict', 'secuencia no creciente')]
            reservation.update(seq=seq, heartbeat=self.world.monotonic())
            return []
        return self._owner_mutation(op_id, key, 'latido', auth, dict(seq=seq), mutate)

    def dudosa(self, op_id, key, reason_text, evidence, auth):
        def mutate(reservation, view):
            if reservation['state'] != 'released':
                reservation['state'] = 'uncertain'
                reservation['reasons'].append(reason('reservation-uncertain', dict(reason=reason_text, evidenceIds=list(evidence))))
            return []
        return self._owner_mutation(op_id, key, 'dudosa', auth, dict(reason=reason_text, evidenceIds=list(evidence)), mutate)

    def _release(self, row, view, auth):
        """soltar efectivo: sella lo nunca consumido, cierra con un barrido Complete propio o retiene."""
        self._fence(row, view, auth, 'soltar')
        creation = creation_from_row(row)
        if row['state'] == 'released': return []  # Cierre original, sin crédito nuevo.
        if type(creation) in (scope.Unissued, scope.Issued):
            epoch = creation.permit.issuer_epoch if type(creation) is scope.Issued else self._issuer_epoch(view)
            self._replace(row, scope.seal(row, store_id=self.config.store_id, issuer_epoch=epoch))
            return []
        if type(creation) is not scope.Registered:
            return [reason('unix-scope-unconfirmed', 'permiso consumido sin raiz registrada: se retiene')]
        observed = self.world.coalition.observe(creation.permit.baseline)
        if observed.kind == 'complete':
            self._replace(row, scope.close(row, observed, store_id=self.config.store_id))
            return []
        # occupied y scan-retry son resultado de esta observación, nunca motivos durables de la fila.
        if observed.kind == 'occupied':
            novel = observed.novel
            reasons = [reason('unix-scope-unconfirmed', 'ambito ocupado por miembros nuevos', True)]
            if any(m.elevated for m in novel): reasons.append(self._doubt(row, 'miembro nuevo elevado en el ambito'))
            status = dict(status='occupied', novel=[[m.birth.pid, m.birth.unique] for m in novel[:64]],
                          novelCount=len(novel))
            return reasons, dict(scopeStatus=status), 1
        if observed.kind == 'retry':
            return ([reason('unix-scope-unconfirmed', 'barrido sin concordancia', True)],
                    dict(scopeStatus=dict(status='scan-retry', reason=observed.reason)), 1)
        reasons = [reason('unix-scope-unconfirmed', 'barrido invalido'), self._doubt(row, 'barrido invalido: ' + observed.reason)]
        return reasons, dict(scopeStatus=dict(status='invalid', reason=observed.reason)), 1

    def soltar(self, op_id, key, evidence, auth):
        def mutate(reservation, view):
            if self.config.mode == 'effective': return self._release(reservation, view, auth)
            if reservation['state'] == 'released': return []
            closure = self.world.closure(evidence, reservation)
            reasons = closure_reasons(closure, reservation, mode=self.config.mode)
            if reasons:
                reservation['state'] = 'uncertain'
                reservation['reasons'].extend(reasons)
            else:
                reservation.update(state='released', reasons=[], closureEvidence=evidence_json(closure))
            return reasons
        return self._owner_mutation(op_id, key, 'soltar', auth, dict(evidenceIds=list(evidence)), mutate)

    def estado(self, query, auth):
        if query: raise ValueError('consulta desconocida')
        if not self._authorized(auth, 'estado'):
            return self._reply(None, [reason('identity-unverifiable', 'consulta no autorizada')], exit_code=5)
        view = self._observe(self.store.read())
        for kind in ('pending', 'reservations'):
            for item in view[kind]:
                # El permiso se muestra sin dueño, canal ni línea base detallada.
                if 'creation' in item: item['permit'] = permit_view(creation_from_row(item))
                item.pop('creation', None)
                item.pop('channelBinding', None)
                for field in ('state', 'effectiveState'):
                    if self.config.mode == 'shadow' and item[field] == 'granted': item[field] = 'simulated'
                if 'heartbeat' in item: item['heartbeatAgeSeconds'] = self.world.monotonic() - item['heartbeat']
        resources = [dict(name=name, holder=r['key'], state=r['effectiveState'])
                     for r in view['reservations'] if r['effectiveState'] != 'released'
                     for name in r['exclusiveResources']]
        health_reasons = self._health()
        return self._reply(None, health_reasons, pending=view['pending'], reservations=view['reservations'],
                           budget=budget_view(self.config, view['reservations']), resources=resources,
                           drained=view['drained'], degraded=bool(health_reasons),
                           physicalHostId=self.config.host_id)
