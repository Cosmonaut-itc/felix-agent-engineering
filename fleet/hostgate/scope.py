"""Frontera de cierre. La evidencia real y los perfiles no están acreditados.

Las formas del permiso de creación (SEN-5650, 4b) existen pero nada las escribe todavía.
darwin_coalition se importa dentro de las funciones: cargar el runtime no lo carga.
"""
from dataclasses import dataclass, field
from typing import Mapping, Optional, Union
from .protocol import canonical, digest, identifier, reason

CREATION_PROTOCOL = 'darwin-coalition-creation@1'
_PERMIT_KEYS = frozenset(('protocol state permitId owner channelBinding boot issuerEpoch confirmationDigest '
                          'launchDigest profileRevision coalitionId baseline baselineDigest baselineCounters').split())
_EXTRA_KEYS = {'issued': (), 'consumed': (), 'registered': ('root', 'scopeReceipt'),
               'closed': ('root', 'scopeReceipt', 'closedBy'), 'sealed': ('closedBy',)}
_ROOT_BOUNDS = dict(pid=(1, 2 ** 31), unique=(1, 2 ** 64), uid=(0, 2 ** 32), ruid=(0, 2 ** 32), svuid=(0, 2 ** 32),
                    parentUnique=(0, 2 ** 64), pidversion=(0, 2 ** 32), coalition=(1, 2 ** 64))


def _require(condition, message='permiso de creacion invalido'):
    if not condition: raise ValueError(message)


def _hex64(value):
    return isinstance(value, str) and len(value) == 64 and set(value) <= set('0123456789abcdef')


@dataclass(frozen=True)
class Permit:
    """Lo que la autorización fija una sola vez; ningún estado posterior lo cambia."""
    permit_id: str
    owner: Mapping
    channel_binding: str
    boot: str
    issuer_epoch: int
    confirmation_digest: str
    launch_digest: str
    profile_revision: str
    baseline: object  # darwin_coalition.Baseline, releída con Baseline.from_json.


@dataclass(frozen=True)
class Unissued:
    """Nunca se autorizó nada: la fila no lleva registro `creation`."""
    state = 'unissued'


@dataclass(frozen=True)
class Issued:
    permit: Permit
    state = 'issued'


@dataclass(frozen=True)
class Consumed:
    permit: Permit
    state = 'consumed'


@dataclass(frozen=True)
class Registered:
    permit: Permit
    root: object  # darwin_coalition.RootIdentity
    scope_receipt: str
    state = 'registered'


@dataclass(frozen=True)
class Closed:
    permit: Permit
    root: object
    scope_receipt: str
    closed_by: str  # evidence_digest de la prueba original.
    state = 'closed'


@dataclass(frozen=True)
class Sealed:
    permit: Optional[Permit]  # None: sellado sin haberse emitido.
    closed_by: str
    state = 'sealed'


Creation = Union[Unissued, Issued, Consumed, Registered, Closed, Sealed]
_LEGAL = {'reserved': (Unissued, Issued, Consumed), 'running': (Registered,),
          'uncertain': (Unissued, Issued, Consumed, Registered), 'released': (Closed, Sealed)}
_LAUNCHED = (Consumed, Registered, Closed)  # launchPossible es histórico desde el consumo.


def root_json(root):
    m = root.member
    return dict(boot=m.birth.boot, pid=m.birth.pid, unique=m.birth.unique, uid=m.uid, ruid=m.ruid, svuid=m.svuid,
                parentUnique=m.parent_unique, pidversion=m.pidversion, coalition=root.coalition)


def root_from_json(value):
    from .darwin_coalition import Birth, Member, RootIdentity
    _require(type(value) is dict and set(value) == set(_ROOT_BOUNDS) | {'boot'} and isinstance(value['boot'], str))
    _require(all(type(value[k]) is int and low <= value[k] < high for k, (low, high) in _ROOT_BOUNDS.items()))
    return RootIdentity(Member(Birth(value['boot'], value['pid'], value['unique']), value['uid'], value['ruid'],
                               value['svuid'], value['parentUnique'], value['pidversion']), value['coalition'])


def creation_json(creation):
    """Forma durable del registro; Unissued no se escribe."""
    if type(creation) is Unissued: return None
    value = dict(protocol=CREATION_PROTOCOL, state=creation.state)
    permit = creation.permit
    if permit is not None:
        b = permit.baseline
        value.update(permitId=permit.permit_id, owner=permit.owner, channelBinding=permit.channel_binding,
                     boot=permit.boot, issuerEpoch=permit.issuer_epoch, confirmationDigest=permit.confirmation_digest,
                     launchDigest=permit.launch_digest, profileRevision=permit.profile_revision, coalitionId=b.coalition,
                     baseline=b.to_json(), baselineDigest=b.digest, baselineCounters=list(b.counters))
    if isinstance(creation, (Registered, Closed)):
        value.update(root=root_json(creation.root), scopeReceipt=creation.scope_receipt)
    if isinstance(creation, (Closed, Sealed)): value['closedBy'] = creation.closed_by
    return value


def _permit(value):
    from .darwin_coalition import Baseline
    _require(type(value['baseline']) is str and isinstance(value['owner'], dict) and type(value['issuerEpoch']) is int
             and value['issuerEpoch'] > 0 and _hex64(value['confirmationDigest']) and _hex64(value['launchDigest']))
    for name in ('permitId', 'channelBinding', 'profileRevision'): identifier(value[name])
    baseline = Baseline.from_json(value['baseline'])
    _require(value['boot'] == baseline.boot and value['coalitionId'] == baseline.coalition
             and value['baselineDigest'] == baseline.digest and value['baselineCounters'] == list(baseline.counters),
             'linea base discordante con el registro')
    return Permit(value['permitId'], value['owner'], value['channelBinding'], value['boot'], value['issuerEpoch'],
                  value['confirmationDigest'], value['launchDigest'], value['profileRevision'], baseline)


def creation_from_row(row):
    """Variante del permiso de una fila. Una forma distinta de la canónica falla; no se repara."""
    if 'creation' not in row: return Unissued()
    value = row['creation']
    _require(type(value) is dict and value.get('protocol') == CREATION_PROTOCOL and value.get('state') in _EXTRA_KEYS)
    state, extra = value['state'], _EXTRA_KEYS[value['state']]
    permit = None
    if not (state == 'sealed' and set(value) == {'protocol', 'state', 'closedBy'}):
        _require(set(value) == _PERMIT_KEYS | set(extra), 'campos del permiso')
        permit = _permit(value)
    root = None
    if 'root' in extra:
        root = root_from_json(value['root'])
        b = permit.baseline
        _require(root.birth.boot == b.boot and root.coalition == b.coalition, 'raiz fuera de la coalicion')
        _require(root.birth not in b.births, 'raiz dentro de la linea base')
        identifier(value['scopeReceipt'])
    if 'closedBy' in extra: _require(_hex64(value['closedBy']))
    creation = {'issued': lambda: Issued(permit), 'consumed': lambda: Consumed(permit),
                'registered': lambda: Registered(permit, root, value['scopeReceipt']),
                'closed': lambda: Closed(permit, root, value['scopeReceipt'], value['closedBy']),
                'sealed': lambda: Sealed(permit, value['closedBy'])}[state]()
    _require(canonical(creation_json(creation)) == canonical(value), 'permiso no canonico')
    return creation


def validate_reservation(row, mode):
    """Formas legales de estado, permiso y prueba por modo; ValueError en cualquier otra."""
    released = row['state'] == 'released'
    _require(('closureEvidence' not in row) or released, 'evidencia en reserva no liberada')
    if mode == 'shadow':
        _require('creation' not in row, 'permiso real en sombra')
    else:
        _require(mode == 'effective')
        creation = creation_from_row(row)
        _require(type(creation) in _LEGAL[row['state']], 'estado y permiso incompatibles')
        _require(row['launchPossible'] is isinstance(creation, _LAUNCHED), 'launchPossible incoherente')
        permit = getattr(creation, 'permit', None)
        if permit is not None:
            _require(canonical(permit.owner) == canonical(row['owner']) and permit.channel_binding == row['channelBinding']
                     and permit.boot == row['physicalBoot'], 'permiso de otro dueno, canal o boot')
    if released:
        _require(not closure_reasons(ClosureEvidence(**row['closureEvidence']), row), 'liberada sin prueba correlacionada')


@dataclass(frozen=True)
class ClosureEvidence:
    kind: str = 'unconfirmed'
    issuer: str = ''
    key: Mapping = field(default_factory=dict)
    boot: str = ''
    scope: str = ''
    provenance: str = ''
    complete: bool = False
    creators_sealed: bool = False
    root_terminal: bool = False
    scope_empty: bool = False
    lineage_complete: bool = False
    profile_verified: bool = False
    resources_clean: bool = False
    permit_revoked: bool = False
    no_external_effects: bool = False
    evidence_digest: str = ''

    @classmethod
    def fixture(cls, key, boot, kind='unix-scope-empty'):
        """Evidencia sintética de harness; nunca acredita un perfil/launcher real."""
        values = dict(kind=kind, issuer='harness', key=key, boot=boot, scope=key['reservationId'],
                      provenance='simulation', complete=True, creators_sealed=True, root_terminal=True,
                      scope_empty=True, lineage_complete=True, profile_verified=True, resources_clean=True,
                      permit_revoked=True, no_external_effects=True)
        return cls(**values, evidence_digest=digest(values))


def closure_reasons(evidence, reservation):
    if not isinstance(evidence, ClosureEvidence):
        return [reason('resource-cleanup-unconfirmed', 'proveedor de cierre no disponible')]
    strings = ('kind', 'issuer', 'boot', 'scope', 'provenance', 'evidence_digest')
    booleans = ('complete', 'creators_sealed', 'root_terminal', 'scope_empty', 'lineage_complete',
                'profile_verified', 'resources_clean', 'permit_revoked', 'no_external_effects')
    if (any(type(getattr(evidence, name)) is not str for name in strings)
            or any(type(getattr(evidence, name)) is not bool for name in booleans)
            or not isinstance(evidence.key, dict)):
        return [reason('resource-cleanup-unconfirmed', 'tipos de evidencia no acreditados')]
    windows = reservation['scopeKind'] == 'windows'
    code = 'windows-tree-unconfirmed' if windows else 'unix-scope-unconfirmed'
    reasons = []
    values = dict(evidence.__dict__); expected_digest = values.pop('evidence_digest')
    try:
        computed_digest = digest(values)
    except (ValueError, TypeError, RecursionError):
        return [reason(code, 'evidencia no serializable')]
    if (not evidence.issuer or evidence.provenance != 'simulation' or not expected_digest
            or computed_digest != expected_digest or evidence.key != reservation['key']
            or evidence.boot != reservation['physicalBoot'] or evidence.scope != reservation['scope']
            or not evidence.complete or not evidence.creators_sealed):
        reasons.append(reason(code, 'emisor/fencing/procedencia/completitud no acreditados'))
    compatible = ('windows-tree-empty', 'never-created') if windows else ('unix-scope-empty', 'never-created')
    if evidence.kind not in compatible:
        reasons.append(reason(code, 'tipo de evidencia ajeno al ambito registrado'))
    if evidence.kind == 'never-created':
        if not evidence.permit_revoked or not evidence.no_external_effects: reasons.append(reason(code, 'permiso o efectos no sellados'))
    elif evidence.kind in ('unix-scope-empty', 'windows-tree-empty'):
        if not evidence.root_terminal or not evidence.scope_empty: reasons.append(reason(code, 'ambito aun no cerrado'))
        if evidence.kind == 'unix-scope-empty' and (not evidence.lineage_complete or not evidence.profile_verified):
            reasons.append(reason(code, 'perfil y linaje obligatorio sin acreditar'))
    else:
        reasons.append(reason(code, 'raiz, pausa, TTL o muestras no prueban cierre'))
    if not evidence.resources_clean: reasons.append(reason('resource-cleanup-unconfirmed', 'limpieza conjunta incompleta'))
    return reasons


def authorize_creation(key, auth):
    raise NotImplementedError('enforcement-unavailable: permiso de creacion futuro')


def record_started(key, scope, auth=None):
    raise NotImplementedError('enforcement-unavailable: supervisor y registro previo futuros')


class ScopeVerifier:
    def closure(self, evidence_ids, reservation):
        return ClosureEvidence()

    def profile_verified(self, profile):
        return False
