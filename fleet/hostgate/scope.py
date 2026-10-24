"""Frontera de cierre. La evidencia real y los perfiles no están acreditados."""
from dataclasses import dataclass, field
from typing import Mapping
from .protocol import digest, reason


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
    windows = evidence.kind.startswith('windows') or reservation.get('scopeKind') == 'windows'
    code = 'windows-tree-unconfirmed' if windows else 'unix-scope-unconfirmed'
    reasons = []
    values = dict(evidence.__dict__); expected_digest = values.pop('evidence_digest')
    if (not evidence.issuer or evidence.provenance != 'simulation' or not expected_digest
            or digest(values) != expected_digest or evidence.key != reservation['key']
            or evidence.boot != reservation['physicalBoot'] or evidence.scope != reservation['key']['reservationId']
            or not evidence.complete or not evidence.creators_sealed):
        reasons.append(reason(code, 'emisor/fencing/procedencia/completitud no acreditados'))
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
