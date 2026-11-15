"""Contrato de datos del tren. Sin reloj, IO ni ejecución de efectos."""
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
from typing import Optional, Tuple, Union


def clave(*partes):
    return hashlib.sha256(json.dumps(partes, ensure_ascii=True,
                                    sort_keys=True).encode()).hexdigest()


class Motivo(str, Enum):
    CUSTODIA = "custodia"
    HEAD = "head-cambiado"
    BASE = "base-no-main"
    CERRADO = "pr-no-abierto"
    APTO = "apto-no-vigente"
    HALLAZGOS = "hallazgos-abiertos"
    DIRIGIDA = "dirigida-no-limpia"
    CI = "ci-no-verde"
    CONFLICTO = "conflicto"
    INCIERTO = "observacion-incompleta"
    VERIFICADO = "falta-verificado"
    PILOTO = "alto-riesgo-excluido-del-piloto"
    MERGE_READY = "merge-ready"


@dataclass(frozen=True)
class Entrega:
    repo: str
    pr: int
    head: str
    apto: str
    dirigida: str

    @property
    def recibo(self):
        return clave("entrega", self.repo, self.pr, self.head)


@dataclass(frozen=True)
class Admitido:
    pass


@dataclass(frozen=True)
class Retenido:
    motivo: Motivo


@dataclass(frozen=True)
class Rechazado:
    motivo: Motivo


Admision = Union[Admitido, Retenido, Rechazado]


@dataclass(frozen=True)
class Entrada:
    entrega: Entrega
    recibida: float
    admision: Admision = Retenido(Motivo.CUSTODIA)
    admitida: Optional[float] = None
    solo: bool = False
    tren: Optional[str] = None


@dataclass(frozen=True)
class Pausa:
    motivo: str
    # Defaults keep events stored before U2 decodable.
    detalle: str = ""


@dataclass(frozen=True)
class Candidato:
    B: str
    eslabones: Tuple[Tuple[int, str, str], ...]
    C: str
    arbol: str


@dataclass(frozen=True)
class ConflictoCadena:
    """Armar C falló al mezclar el PR `pr` sobre el eslabón anterior."""
    B: str
    pr: int
    archivos: Tuple[str, ...]


@dataclass(frozen=True)
class Publicado:
    C: str


@dataclass(frozen=True)
class RamaOcupada:
    C: str
    observado: str


Publicacion = Union[Publicado, RamaOcupada]


@dataclass(frozen=True)
class Aterrizado:
    C: str


@dataclass(frozen=True)
class EnBase:
    B: str


@dataclass(frozen=True)
class EscritorExterno:
    main: str


@dataclass(frozen=True)
class Incierto:
    motivo: str


# Lectura de refs/heads/main del remoto tras cada intento de promoción.
Reconciliacion = Union[Aterrizado, EnBase, EscritorExterno, Incierto]


@dataclass(frozen=True)
class Fallo:
    unidad: str
    firma: str
    clase: str


@dataclass(frozen=True)
class Completo:
    veredicto: str
    fallos: Tuple[Fallo, ...] = ()


@dataclass(frozen=True)
class Incompleto:
    codigo: int = 75
    reanudar: str = ""


ResultadoSuite = Union[Completo, Incompleto]


@dataclass(frozen=True)
class Atribucion:
    minimos: Tuple[Tuple[int, ...], ...]


@dataclass(frozen=True)
class Congelado:
    B: str


@dataclass(frozen=True)
class Armado:
    candidato: Candidato


@dataclass(frozen=True)
class EsperandoCI:
    candidato: Candidato


@dataclass(frozen=True)
class Corriendo:
    candidato: Candidato
    suite: str


@dataclass(frozen=True)
class Certificado:
    C: str
    arbol: str
    suite: str
    ci: str
    B: str = ""


@dataclass(frozen=True)
class Diagnosticando:
    candidato: Candidato
    fallos: Tuple[Fallo, ...]


@dataclass(frozen=True)
class Promoviendo:
    certificado: Certificado
    intentos: int = 1


@dataclass(frozen=True)
class Terminal:
    resultado: str


Fase = Union[Congelado, Armado, EsperandoCI, Corriendo, Certificado,
             Diagnosticando, Promoviendo, Terminal]


@dataclass(frozen=True)
class Tren:
    id: str
    recibos: Tuple[str, ...]
    fase: Fase


@dataclass(frozen=True)
class Estado:
    repo: str
    entradas: Tuple[Entrada, ...] = ()
    trenes: Tuple[Tren, ...] = ()
    pausa: Optional[Pausa] = None


def inicial(repo):
    return Estado(repo, pausa=Pausa("etapa-1")
                  if repo.rsplit("/", 1)[-1].lower() == "jale" else None)


@dataclass(frozen=True)
class VistaPR:
    pr: int
    head: str
    base: str = "main"
    abierto: bool = True
    apto: bool = False
    hallazgos: Optional[int] = None
    dirigida: bool = False
    ci: str = "pendiente"
    conflicto: Optional[bool] = None
    sensible: bool = False
    alto_riesgo: bool = False
    verificado: bool = False
    autorizado: bool = False
    completa: bool = True
    # A bound view cannot be reused for a new delivery of the same PR.
    recibo: str = ""


@dataclass(frozen=True)
class Observacion:
    ahora: float
    base: str
    prs: Tuple[VistaPR, ...]
    excluir_alto_riesgo: bool = True
    candidato: Optional[Union[Candidato, ConflictoCadena]] = None
    ci_candidato: str = "pendiente"
    suite: Optional[str] = None
    publicacion: Optional[Publicacion] = None
    # Sólo el ejecutor de `promover` la llena; Observacion.base no se reutiliza.
    promocion: Optional[Reconciliacion] = None


@dataclass(frozen=True)
class Efecto:
    clave: str
    tipo: str
    datos: Tuple[str, ...]


LIMITE_PROMOCION = 3


def _con_fase(estado, tren, fase, **cambios):
    return replace(estado, trenes=tuple(replace(t, fase=fase) if t.id == tren.id else t
                                        for t in estado.trenes), **cambios)


def _promoviendo(estado, tren, observacion, efectos):
    """Sólo la lectura de main decide: el push pudo haber llegado ya."""
    fase = tren.fase
    cert = fase.certificado
    lectura = observacion.promocion
    if lectura is None or isinstance(lectura, Incierto):
        return estado, tuple(efectos)
    if isinstance(lectura, Aterrizado) and lectura.C == cert.C:
        return _con_fase(estado, tren, Terminal("ATERRIZADO")), tuple(efectos)
    if isinstance(lectura, EnBase) and lectura.B == cert.B:
        if fase.intentos >= LIMITE_PROMOCION:
            return replace(estado, pausa=Pausa("promocion-rechazada",
                                               "intentos={}".format(fase.intentos))), tuple(efectos)
        intento = fase.intentos + 1
        efectos.append(Efecto(clave("promover", tren.id, intento), "promover", (tren.id,)))
        return _con_fase(estado, tren, Promoviendo(cert, intento)), tuple(efectos)
    if isinstance(lectura, EscritorExterno) and lectura.main not in (cert.B, cert.C):
        # Members keep `admitida`, so the FIFO order survives the rearm.
        entradas = tuple(replace(e, tren=None) if e.tren == tren.id else e
                         for e in estado.entradas)
        return _con_fase(estado, tren, Terminal("REARMAR"), entradas=entradas,
                         pausa=Pausa("escritor-externo", "main=" + lectura.main)), tuple(efectos)
    return replace(estado, pausa=Pausa("observacion-no-corresponde")), tuple(efectos)


def admision(vista, observacion):
    if not vista.abierto:
        return Rechazado(Motivo.CERRADO)
    if vista.base != "main":
        return Rechazado(Motivo.BASE)
    if not vista.completa:
        return Retenido(Motivo.INCIERTO)
    if not vista.apto:
        return Retenido(Motivo.APTO)
    if vista.hallazgos != 0:
        return Retenido(Motivo.HALLAZGOS)
    if not vista.dirigida:
        return Retenido(Motivo.DIRIGIDA)
    if vista.ci != "verde":
        return Retenido(Motivo.CI)
    if vista.conflicto is None:
        return Retenido(Motivo.INCIERTO)
    if vista.conflicto:
        return Retenido(Motivo.CONFLICTO)
    if vista.sensible and not vista.autorizado:
        return Retenido(Motivo.MERGE_READY)
    if vista.alto_riesgo and not vista.verificado:
        return Retenido(Motivo.VERIFICADO)
    if vista.alto_riesgo and observacion.excluir_alto_riesgo:
        return Retenido(Motivo.PILOTO)
    return Admitido()


def paso(estado, observacion):
    """Un paso determinista. Congela a lo sumo un lote y emite sólo datos."""
    efectos = []
    entradas = []
    vistas = {v.recibo or str(v.pr): v for v in observacion.prs}
    for entrada in estado.entradas:
        entrega = entrada.entrega
        vista = vistas.get(entrega.recibo, vistas.get(str(entrega.pr)))
        if isinstance(entrada.admision, Rechazado):
            entradas.append(entrada)
            continue
        if vista is not None and vista.head != entrega.head:
            entrada = replace(entrada, admision=Rechazado(Motivo.HEAD))
            efectos.append(Efecto(clave("caduca", entrega.recibo),
                                  "avisar-caducidad", (entrega.recibo,)))
        else:
            decision = (admision(vista, observacion) if vista else
                        Retenido(Motivo.INCIERTO))
            if isinstance(decision, Admitido):
                admitida = (entrada.admitida if entrada.admitida is not None
                            else observacion.ahora)
            elif decision == Retenido(Motivo.INCIERTO):
                # A failed read is not negative evidence: keep window and FIFO place.
                admitida = entrada.admitida
            else:
                admitida = None
            entrada = replace(entrada, admision=decision, admitida=admitida,
                              solo=bool(vista and (vista.sensible or vista.alto_riesgo)))
        entradas.append(entrada)
    estado = replace(estado, entradas=tuple(entradas))
    if estado.pausa:
        return estado, tuple(efectos)
    activos = [t for t in estado.trenes if not isinstance(t.fase, Terminal)]
    if activos:
        tren = activos[0]
        if isinstance(tren.fase, Promoviendo):
            return _promoviendo(estado, tren, observacion, efectos)
        miembros = [e for e in entradas if e.entrega.recibo in tren.recibos]
        if any(isinstance(e.admision, Rechazado) for e in miembros):
            return replace(estado, pausa=Pausa("head-cambiado-en-tren")), tuple(efectos)
        if any(not isinstance(e.admision, Admitido) for e in miembros):
            return estado, tuple(efectos)
        fase = tren.fase
        if isinstance(fase, Congelado) and observacion.candidato:
            candidato = observacion.candidato
            por_recibo = {e.entrega.recibo: e for e in miembros}
            esperados = tuple((por_recibo[r].entrega.pr, por_recibo[r].entrega.head)
                              for r in tren.recibos)
            if isinstance(candidato, ConflictoCadena):
                if candidato.B != fase.B or candidato.pr not in {pr for pr, _ in esperados}:
                    return replace(estado, pausa=Pausa("candidato-no-corresponde")), tuple(efectos)
                return replace(estado, pausa=Pausa("conflicto-en-cadena", "pr={} archivos={}".format(
                    candidato.pr, ",".join(candidato.archivos)))), tuple(efectos)
            if (candidato.B != fase.B or
                    tuple((pr, head) for pr, head, _ in candidato.eslabones) != esperados):
                return replace(estado, pausa=Pausa("candidato-no-corresponde")), tuple(efectos)
            fase = Armado(candidato)
        elif isinstance(fase, Armado):
            fase = EsperandoCI(fase.candidato)
            efectos.append(Efecto(clave("ci", tren.id), "solicitar-ci", (tren.id,)))
        elif (isinstance(fase, EsperandoCI) and isinstance(observacion.publicacion, RamaOcupada)
              and observacion.publicacion.C == fase.candidato.C):
            return replace(estado, pausa=Pausa("rama-tren-ocupada",
                                               "sha=" + observacion.publicacion.observado)), tuple(efectos)
        elif isinstance(fase, EsperandoCI) and observacion.ci_candidato == "verde":
            efectos.append(Efecto(clave("suite", tren.id), "iniciar-suite", (tren.id,)))
            if observacion.suite:
                fase = Corriendo(fase.candidato, observacion.suite)
        elif isinstance(fase, Certificado):
            # U4 builds Certificado; a pre-U2 one without B cannot promote.
            if not fase.B:
                return replace(estado, pausa=Pausa("certificado-sin-base")), tuple(efectos)
            fase = Promoviendo(fase, 1)
            efectos.append(Efecto(clave("promover", tren.id, 1), "promover", (tren.id,)))
        return _con_fase(estado, tren, fase), tuple(efectos)
    fila = sorted((e for e in entradas if isinstance(e.admision, Admitido)
                   and e.tren is None), key=lambda e: (e.admitida, e.recibida))
    lote = []
    for entrada in fila:
        if entrada.solo:
            if not lote:
                lote.append(entrada)
            break
        lote.append(entrada)
        if len(lote) == 3:
            break
    if not lote or not observacion.base:
        return estado, tuple(efectos)
    if not (lote[0].solo or len(lote) == 3 or
            observacion.ahora - lote[0].admitida >= 3600):
        return estado, tuple(efectos)
    recibos = tuple(e.entrega.recibo for e in lote)
    tren_id = clave("tren", estado.repo, observacion.base, recibos)
    if any(t.id == tren_id for t in estado.trenes):
        # A rearm over the same main (main came back to B) needs fresh effect keys.
        tren_id = clave("tren", estado.repo, observacion.base, recibos, len(estado.trenes))
    tren = Tren(tren_id, recibos, Congelado(observacion.base))
    estado = replace(estado, entradas=tuple(replace(e, tren=tren_id)
                     if e.entrega.recibo in recibos else e for e in entradas),
                     trenes=estado.trenes + (tren,))
    efectos.append(Efecto(clave("armar", tren_id), "armar-candidato", (tren_id,)))
    return estado, tuple(efectos)


# Tagged encoding keeps the SQLite/event contract explicit and round-trippable.
_TIPOS = {c.__name__: c for c in (Entrega, Admitido, Retenido, Rechazado, Entrada,
          Pausa, Candidato, Fallo, Completo, Incompleto, Atribucion, Congelado,
          Armado, EsperandoCI, Corriendo, Certificado, Diagnosticando,
          Promoviendo, Terminal, Tren, Estado, Efecto, ConflictoCadena,
          Publicado, RamaOcupada, Aterrizado, EnBase, EscritorExterno, Incierto)}


def datos(valor):
    from dataclasses import fields, is_dataclass
    if is_dataclass(valor):
        return dict({"_tipo": type(valor).__name__},
                    **{f.name: datos(getattr(valor, f.name)) for f in fields(valor)})
    if isinstance(valor, tuple):
        return [datos(v) for v in valor]
    return valor.value if isinstance(valor, Enum) else valor


def desde_datos(valor):
    if isinstance(valor, list):
        return tuple(desde_datos(v) for v in valor)
    if isinstance(valor, dict):
        tipo = _TIPOS[valor["_tipo"]]
        kwargs = {k: desde_datos(v) for k, v in valor.items() if k != "_tipo"}
        if tipo in (Retenido, Rechazado):
            kwargs["motivo"] = Motivo(kwargs["motivo"])
        return tipo(**kwargs)
    return valor
