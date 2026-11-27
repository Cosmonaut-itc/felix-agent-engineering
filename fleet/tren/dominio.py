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
    terminal: Optional['Terminal'] = None
    # Sólo doble camino: instante observado de caducidad, sin borrar admisión.
    caducada: Optional[float] = None
    # Ligada al recibo: conserva el aislamiento mientras se busca al culpable.
    partida: bool = False
    # Liberada por rearme temprano o al resolver una partición: conserva su
    # FIFO hasta entrar al lote siguiente.
    rearmada: bool = False
    particion: Optional[str] = None
    rearme_ci: Optional[str] = None
    ci_ref_nueva: bool = False
    # Sin prefijo espera otra base. Con prefijo evita repetir la combinación.
    aplazada_en_base: str = ""
    prefijo_en_conflicto: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Pausa:
    motivo: str
    pr: Optional[int] = None
    main: str = ""
    archivos: Tuple[str, ...] = ()
    detalle: str = ""


@dataclass(frozen=True)
class Decision:
    id: str
    repo: str
    tren: Optional[str]
    decision: str
    pr: Optional[int]
    motivo: str
    instante: float
    pausa_id: str
    pausa: Optional[Pausa]


@dataclass(frozen=True)
class Candidato:
    B: str
    eslabones: Tuple[Tuple[int, str, str], ...]
    C: str
    arbol: str


@dataclass(frozen=True)
class ConflictoCadena:
    pr: int
    archivos: Tuple[str, ...]


@dataclass(frozen=True)
class RamaOcupada:
    ref: str
    esperado: str
    actual: str


@dataclass(frozen=True)
class Aterrizado:
    B: str
    C: str
    arbol: str


@dataclass(frozen=True)
class EnBase:
    B: str
    C: str


@dataclass(frozen=True)
class EscritorExterno:
    B: str
    C: str
    main: str


@dataclass(frozen=True)
class EsperaPrevia:
    B: str
    C: str


@dataclass(frozen=True)
class BloqueoPrevia:
    B: str
    C: str
    motivo: str


@dataclass(frozen=True)
class VentanaAutorizada:
    inicio: float
    fin: float


@dataclass(frozen=True)
class Incierto:
    B: str
    C: str
    motivo: str


Reconciliacion = Union[Aterrizado, EnBase, EscritorExterno, Incierto,
                       EsperaPrevia, BloqueoPrevia]


@dataclass(frozen=True)
class Fallo:
    unidad: str
    firma: str
    clase: str
    check: str = ""
    etapa: str = ""
    parte: str = ""
    duracion_s: float = 0


LANZADOR_JALE = "scripts/test-hosts/run-remote.sh"
LANZADOR_CAJA = "scripts/tren/lanzador"
PARTE_CAJA = "caja-desk"


def argv_reproduccion(fallo, commit, origen="<resultado.json>", worktree="<worktree>"):
    if fallo.parte == "jale":
        return ["bash", LANZADOR_JALE, "auto", "<log>", fallo.check, commit, fallo.unidad]
    return argv_repetir_publico(fallo.parte, fallo.check, fallo.unidad, commit, origen, worktree)


def argv_repetir_publico(parte, check, unidad, commit, origen, worktree):
    """Plantilla de repetición con el contrato de Hasito; caja exige evidencia y nombre."""
    args = ["--repetir", origen, "--sha", commit, "--worktree", worktree, "--veces", "2",
            "--unidad", check + "=" + unidad]
    if parte == PARTE_CAJA:
        return ["bash", LANZADOR_CAJA, *args, "--evidencia", "<evidencia>", "--nombre", "<nombre>"]
    return ["bash", "scripts/mini/run.sh", *args]


def parte_publica(fallo):
    """Sólo caja viaja con su parte: los payloads guardados de Hasito y Jale no cambian."""
    return {"parte": fallo.parte} if fallo.parte == PARTE_CAJA else {}


@dataclass(frozen=True)
class Completo:
    veredicto: str
    fallos: Tuple[Fallo, ...] = ()
    costo_s: float = 0
    restantes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Incompleto:
    codigo: int = 75
    reanudar: str = ""


@dataclass(frozen=True)
class RechazoLanzador:
    evidencia: str
    detalle: str = "lanzador-rechazo"


@dataclass(frozen=True)
class Intento:
    n: int
    veredicto: str
    firma: str
    duracion_s: float
    ensayo: str = ""
    huella_entorno: str = ""
    caso_ejecutado: bool = False


@dataclass(frozen=True)
class UnidadRepetida:
    check: str
    unidad: str
    parte: str
    definicion: str
    intentos: Tuple[Intento, ...]


@dataclass(frozen=True)
class Repeticion:
    sha: str
    origen_sha: str
    base: str
    origen_sha256: str
    unidades: Tuple[UnidadRepetida, ...]
    comando: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Reproduccion:
    id: str
    fallo: Fallo
    objetivo: str
    prs: Tuple[int, ...] = ()
    costo_s: float = 0


@dataclass(frozen=True)
class Evidencia:
    solicitud: Reproduccion
    resultado: Repeticion


def costo_atribucion(evidencias, en_paralelo=False, *, siguiente=None):
    costos = [(e.solicitud, max(e.solicitud.costo_s,
        sum(i.duracion_s for u in e.resultado.unidades for i in u.intentos)))
        for e in evidencias]
    if siguiente is not None:
        costos.append((siguiente, siguiente.costo_s))
    if not en_paralelo:
        return sum(costo for _, costo in costos)
    tandas = {}
    for solicitud, costo in costos:
        grupo = len(solicitud.prs) if solicitud.objetivo == "subconjunto" else 0
        tandas[grupo] = max(tandas.get(grupo, 0), costo)
    return sum(tandas.values())


@dataclass(frozen=True)
class Causa:
    fallo: Fallo
    minimos: Tuple[Tuple[int, ...], ...]


ResultadoSuite = Union[Completo, Incompleto, RechazoLanzador]


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
    desde: Optional[float] = None
    solicitud: int = 0


@dataclass(frozen=True)
class ResultadoCI:
    estado: str
    rojos: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Corriendo:
    candidato: Candidato
    suite: str
    ci: str = "verde"
    reanudaciones: int = 0
    esperar_hasta: Optional[float] = None
    reanudar: str = ""
    reintentos_lanzador: int = 0
    reenganches: int = 0
    registro_reenganche: str = ""


@dataclass(frozen=True)
class Certificado:
    C: str
    arbol: str
    suite: str
    ci: str
    # Defaults keep snapshots/events written by U1 readable.
    B: str = ""
    ci_pendiente_desde: Optional[float] = None


@dataclass(frozen=True)
class Diagnosticando:
    candidato: Candidato
    fallos: Tuple[Fallo, ...]
    suite: str = ""
    ci: str = "verde"
    presupuesto_s: float = 0
    evidencias: Tuple[Evidencia, ...] = ()
    pendiente: Optional[Reproduccion] = None
    subconjunto: Optional[Candidato] = None
    reanudaciones: int = 0
    esperar_hasta: Optional[float] = None
    reanudar: str = ""
    costo_infra_s: float = 0
    restantes: Tuple[str, ...] = ()
    reintentos_lanzador: int = 0
    reenganches: int = 0
    registro_reenganche: str = ""


@dataclass(frozen=True)
class Promoviendo:
    certificado: Certificado
    intentos: int = 1
    # EnBase ya recibido; la admisión puede diferir el siguiente intento.
    reintento_pendiente: bool = False
    reintento_autorizado: bool = False


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
    costo_suite_sana_s: float = 0


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
    # Sólo la sombra interpreta un merge externo; default compatible con U1.
    mergeado: bool = False
    ventana: Optional[VentanaAutorizada] = None
    sensible_en_lote: bool = False
    alto_riesgo_en_lote: bool = False

    @property
    def requiere_solo(self):
        return ((self.alto_riesgo and not self.alto_riesgo_en_lote)
                or (self.sensible and not self.sensible_en_lote))


@dataclass(frozen=True)
class Observacion:
    ahora: float
    base: str
    prs: Tuple[VistaPR, ...]
    excluir_alto_riesgo: bool = True
    candidato: Optional[Candidato] = None
    ci_candidato: str = "pendiente"
    suite: Optional[str] = None
    conflicto_cadena: Optional[ConflictoCadena] = None
    main: Optional[Reconciliacion] = None
    tren_id: str = ""
    intento_promocion: int = 0
    rama_ocupada: Optional[RamaOcupada] = None
    resultado_suite: Optional[ResultadoSuite] = None
    suite_intento: int = 0
    repeticion: Optional[Union[Repeticion, Incompleto, RechazoLanzador]] = None
    reproduccion_id: str = ""
    costo_suite_sana_s: float = 0
    ci_recibo: str = "verde"
    decision: Optional[Decision] = None
    pausa_id: str = ""
    ci_rojos: Tuple[str, ...] = ()
    absoluciones_recientes: Tuple[Tuple[str, int], ...] = ()
    archivos_lote: Optional[Tuple[str, ...]] = None
    firma_distingue_fallo: bool = False
    repeticiones_en_paralelo: bool = False
    # El efecto iniciar-suite del tren activo ya empezó su IO en algún tick.
    suite_iniciada: bool = False
    # Ese IO empezó con la solicitud de CI vigente: su plazo ya no aplica.
    suite_de_ci_vigente: bool = False


@dataclass(frozen=True)
class Efecto:
    clave: str
    tipo: str
    datos: Tuple[str, ...]


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
    # merge.md: Felix's per-head authorization admits high risk, even in the pilot.
    if vista.alto_riesgo and not vista.autorizado:
        return Retenido(Motivo.PILOTO if observacion.excluir_alto_riesgo
                        else Motivo.MERGE_READY)
    return Admitido()


def admision_entrada(entrada, vistas, observacion):
    """Misma regla para el reductor y la barrera previa al push."""
    if isinstance(entrada.admision, Rechazado):
        return entrada.admision
    entrega = entrada.entrega
    vista = vistas.get(entrega.recibo, vistas.get(str(entrega.pr)))
    if vista is None:
        return Retenido(Motivo.INCIERTO)
    if vista.head != entrega.head:
        return Rechazado(Motivo.HEAD)
    return admision(vista, observacion)


LIMITE_PROMOCION = 3


def efecto_promover(tren_id, intentos):
    return Efecto(clave("promover", tren_id, intentos), "promover",
                  (tren_id, str(intentos)))


def efectos_status(tren, tipo):
    """Una señal durable por entrega y generación, sin IO ni comentario."""
    return tuple(Efecto(clave(tipo, tren.id, recibo), tipo, (tren.id, recibo))
                 for recibo in tren.recibos)


def pausar_previa(estado, motivo):
    return replace(estado, pausa=Pausa("infraestructura", detalle="pre-aterrizaje:" + motivo))


def aterrizaje(estado, tren, observacion):
    """Promover tras admisión; reconciliar antes, pues el PR puede estar cerrado."""
    fase = tren.fase
    efectos = ()
    if isinstance(fase, Certificado):
        if not fase.B:
            return replace(estado, pausa=Pausa("certificado-sin-base")), ()
        fase = Promoviendo(fase)
        efectos = (efecto_promover(tren.id, fase.intentos),)
    else:
        cert = fase.certificado
        lectura = observacion.main
        # A delayed observation must not consume another attempt/train's result.
        if (observacion.tren_id != tren.id or
                observacion.intento_promocion != fase.intentos or
                lectura is None or (lectura.B, lectura.C) != (cert.B, cert.C)):
            return estado, ()
        if isinstance(lectura, Aterrizado) and lectura.arbol == cert.arbol:
            fase = Terminal("ATERRIZADO")
            efectos = efectos_status(tren, "avisar-aterrizaje")
            miembros = tuple(e for e in estado.entradas if e.entrega.recibo in tren.recibos)
            estado = replace(estado, entradas=tuple(
                replace(e, rearme_ci=None, ci_ref_nueva=False) if e in miembros
                else e for e in estado.entradas))
            estado = cambiar_fase(estado, tren, fase)
            return liberar_particiones(estado, miembros), efectos
        elif isinstance(lectura, BloqueoPrevia):
            return pausar_previa(estado, lectura.motivo), ()
        elif isinstance(lectura, EnBase):
            fase = replace(fase, reintento_pendiente=True)
        elif isinstance(lectura, EscritorExterno):
            return replace(rearmar(estado, tren),
                           pausa=Pausa("escritor-externo", main=lectura.main)), ()
        else:
            return estado, ()
    return replace(estado, trenes=tuple(replace(t, fase=fase) if t.id == tren.id
                                       else t for t in estado.trenes)), efectos


def rearmar(estado, tren, temprano=False, rojo_ci=False):
    """Cierra el tren y devuelve sus miembros a la fila.

    Las fechas FIFO no cambian; el reductor congela el lote siguiente sobre main.
    """
    return replace(estado,
        trenes=tuple(replace(t, fase=Terminal("REARMAR")) if t.id == tren.id else t
                     for t in estado.trenes),
        entradas=tuple(replace(e, tren=None, rearmada=temprano or e.rearmada,
                               rearme_ci=tren.id if rojo_ci else e.rearme_ci)
                       if e.entrega.recibo in tren.recibos
                       else e for e in estado.entradas))


def base_vieja(fase, observacion):
    """main ya no es la B de un tren cuya suite aún no empezó.

    Una suite iniciada puede tener coordinadores vivos aunque la fase siga en
    EsperandoCI; cerrar el tren los dejaría sin dueño.
    """
    if isinstance(fase, Congelado):
        base = fase.B
    elif isinstance(fase, Armado) or (isinstance(fase, EsperandoCI) and
                                      not observacion.suite_iniciada):
        base = fase.candidato.B
    else:
        return False
    return bool(observacion.base) and observacion.base != base


ESPERAS_SUITE = (300, 900, 3600)


def cambiar_fase(estado, tren, fase):
    return replace(estado, trenes=tuple(replace(t, fase=fase) if t.id == tren.id else t
                                       for t in estado.trenes))


def rama_ci(tren):
    """Otra solicitud manual publica otra ref sin modificar el C probado."""
    solicitud = getattr(tren.fase, "solicitud", 0)
    return tren.id + ("-ci-" + str(solicitud) if solicitud else "")


def recibo_ci(tren):
    """Identidad durable de C y, al reanudar, de la solicitud publicada."""
    cert = certificado_de(tren.fase)
    if cert is not None:
        inicial = "github:" + cert.C
        prefijo = inicial + ":tren/" + tren.id + "-ci-"
        numero = cert.ci[len(prefijo):] if cert.ci.startswith(prefijo) else ""
        return cert.ci if numero.isascii() and numero.isdigit() and numero[0] != "0" else inicial
    return "github:" + tren.fase.candidato.C + (
        ":tren/" + rama_ci(tren) if tren.fase.solicitud else "")


def certificado_de(fase):
    if isinstance(fase, Promoviendo):
        return fase.certificado
    return fase if isinstance(fase, Certificado) else None


def cambiar_plazo_ci_promocion(estado, tren, desde):
    cert = replace(certificado_de(tren.fase), ci_pendiente_desde=desde)
    fase = replace(tren.fase, certificado=cert) if isinstance(tren.fase, Promoviendo) else cert
    return cambiar_fase(estado, tren, fase)


def solo_por_vista(vista):
    return bool(vista and (vista.requiere_solo or vista.ventana is not None))


def liberar_particiones(estado, resueltas):
    particiones = {e.particion for e in resueltas if e.particion is not None}
    if not particiones:
        return estado
    terminales = {t.id for t in estado.trenes if isinstance(t.fase, Terminal)}
    return replace(estado, entradas=tuple(
        replace(e, partida=False, particion=None, rearmada=True)
        if e.particion in particiones and e.tren not in terminales and e.terminal is None
        and not isinstance(e.admision, Rechazado) else e
        for e in estado.entradas))


SALIDAS_CI = {"verde": "suite", "rojo": "separar-o-devolver", "pendiente": "esperar"}
PLAZO_CI = 3600


def lectura_ci(tren, observacion):
    if observacion.tren_id != tren.id or observacion.ci_recibo != recibo_ci(tren):
        return "pendiente"
    if observacion.ci_candidato == "rojo" and not (
            isinstance(observacion.ci_rojos, (tuple, list)) and observacion.ci_rojos and
            all(isinstance(n, str) and n.strip() for n in observacion.ci_rojos)):
        return "pendiente"
    return observacion.ci_candidato


def cerrar_por_ci_rojo(estado, tren, B, C, rojos):
    if len(tren.recibos) > 1:
        if not any(e.rearme_ci for e in estado.entradas if e.entrega.recibo in tren.recibos):
            return rearmar(estado, tren, temprano=True, rojo_ci=True), ()
        estado = cambiar_fase(estado, tren, Terminal("PARTIDO"))
        return replace(estado, entradas=tuple(
            replace(e, tren=None, solo=True, partida=True, particion=tren.id)
            if e.entrega.recibo in tren.recibos else e for e in estado.entradas)), ()
    entrada = next(e for e in estado.entradas if e.entrega.recibo == tren.recibos[0])
    entrega = entrada.entrega
    payload = json.dumps({"lote": tren.id, "B": B, "C": C,
        "pr": entrega.pr, "head": entrega.head, "fallos": [], "ci_rojos": rojos}, sort_keys=True)
    efecto = Efecto(clave("devolver", tren.id, entrega.recibo), "devolver",
                    (tren.id, entrega.recibo, payload))
    estado = cambiar_fase(estado, tren, Terminal("DEVUELTO"))
    estado = replace(estado, entradas=tuple(
        replace(e, terminal=Terminal("DEVUELTO"), rearme_ci=None, ci_ref_nueva=False)
        if e == entrada else e
        for e in estado.entradas))
    return liberar_particiones(estado, (entrada,)), (efecto,)


def avanzar_ci_promocion(estado, tren, observacion):
    cert = certificado_de(tren.fase)
    lectura = lectura_ci(tren, observacion)
    if lectura == "verde":
        if cert.ci_pendiente_desde is not None:
            estado = cambiar_plazo_ci_promocion(estado, tren, None)
        return estado, (), True
    if lectura == "rojo":
        nuevo, efectos = cerrar_por_ci_rojo(estado, tren, cert.B, cert.C, observacion.ci_rojos)
        return nuevo, efectos, False
    if cert.ci_pendiente_desde is None:
        estado = cambiar_plazo_ci_promocion(estado, tren, observacion.ahora)
    elif observacion.ahora - cert.ci_pendiente_desde >= PLAZO_CI:
        estado = replace(estado, pausa=Pausa("infraestructura",
                                            detalle="pre-aterrizaje:ci-sin-resultado"))
    return estado, (), False


def avanzar_ci(estado, tren, observacion):
    """Salida pura de EsperandoCI; el rojo exige identidad de tren y C."""
    fase = tren.fase
    if fase.desde is None:
        # Snapshots anteriores a U16: empezar el plazo al primer tick nuevo.
        fase = replace(fase, desde=observacion.ahora)
        estado = cambiar_fase(estado, tren, fase)
    lectura = lectura_ci(tren, observacion)
    salida = SALIDAS_CI.get(lectura, "esperar")
    if salida == "separar-o-devolver":
        return cerrar_por_ci_rojo(estado, tren, fase.candidato.B, fase.candidato.C, observacion.ci_rojos)
    if (salida == "esperar" and not observacion.suite_de_ci_vigente and
            observacion.ahora - fase.desde >= PLAZO_CI):
        return replace(estado, pausa=Pausa("infraestructura", detalle="ci-sin-resultado")), ()
    return estado, ()


def intento_suite(fase):
    """Identidad del registro; los rechazos no gastan reanudaciones de 75."""
    return fase.reanudaciones + fase.reintentos_lanzador


def efecto_reanudar_suite(tren, fase):
    intento = intento_suite(fase)
    return efecto_corrida("reanudar-suite", (tren.id,), intento, fase)


def efecto_corrida(tipo, datos, intento, fase):
    partes = (tipo, *datos, intento)
    datos = (*datos, str(intento))
    if fase.reenganches:
        partes += (fase.reenganches,)
        datos += (str(fase.reenganches), fase.registro_reenganche)
    return Efecto(clave(*partes), tipo, datos)


def pausar_lanzador(estado, resultado):
    archivos = (resultado.evidencia,)
    if resultado.detalle == "salida-sin-recibo":
        pausa = Pausa("infraestructura", detalle="salida-sin-recibo", archivos=archivos)
    elif resultado.detalle == "corrida-interrumpida":
        pausa = Pausa("infraestructura", detalle="corrida-interrumpida", archivos=archivos)
    elif resultado.detalle == "recibo-malformado":
        pausa = Pausa("infraestructura", detalle="recibo-malformado", archivos=archivos)
    elif resultado.detalle == "cobertura-parcial-boat":
        pausa = Pausa("infraestructura", detalle="cobertura-parcial-boat", archivos=archivos)
    else:
        pausa = Pausa("infraestructura", detalle="lanzador-rechazo", archivos=archivos)
    return replace(estado, pausa=pausa), ()


def efecto_diagnostico(tren, fase):
    solicitud = fase.pendiente
    tipo = ("armar-subconjunto" if solicitud.objetivo == "subconjunto" and
            fase.subconjunto is None else "repetir")
    return efecto_corrida(tipo, (tren.id, solicitud.id), intento_suite(fase), fase)


def esperar_infraestructura(estado, tren, fase, resultado, ahora):
    if resultado.codigo != 75 or not resultado.reanudar:
        return replace(estado, pausa=Pausa("necesita-juicio", detalle="incompleto-invalido")), ()
    if fase.reanudaciones >= len(ESPERAS_SUITE):
        estado = cambiar_fase(estado, tren, replace(fase, reanudar=resultado.reanudar))
        return replace(estado, pausa=Pausa("infraestructura", detalle="tres-reanudaciones-agotadas")), ()
    fase = replace(fase, esperar_hasta=ahora + ESPERAS_SUITE[fase.reanudaciones],
                   reanudar=resultado.reanudar)
    return cambiar_fase(estado, tren, fase), ()


def devolver_atribuidos(estado, tren, fase, causas):
    """Un efecto por entrega; todos los fallos conservan sus conjuntos causales."""
    import shlex
    culpables = {pr for causa in causas for grupo in causa.minimos for pr in grupo}
    entradas = {e.entrega.recibo: e for e in estado.entradas}
    efectos = []
    for recibo in tren.recibos:
        entrega = entradas[recibo].entrega
        if entrega.pr not in culpables:
            continue
        detalles = []
        for causa in causas:
            for grupo in causa.minimos:
                if entrega.pr not in grupo:
                    continue
                f = causa.fallo
                # Plantilla dirigida: el SHA/árbol causal se resuelve en el
                # expediente de armar-subconjunto. No incorpora una shell.
                solicitud = Reproduccion(clave("reproduccion", (f.parte, f.check, f.unidad),
                                               "subconjunto", grupo), f, "subconjunto", grupo)
                evidencia = next((e for e in fase.evidencias if e.solicitud.id == solicitud.id), None)
                if evidencia is None:
                    evidencia = next(e for e in fase.evidencias if
                        e.solicitud.objetivo == "C" and e.solicitud.fallo.unidad == f.unidad
                        and e.solicitud.fallo.check == f.check and e.solicitud.fallo.parte == f.parte)
                commit = evidencia.resultado.sha
                comando = shlex.join(evidencia.resultado.comando or argv_reproduccion(
                    f, commit, fase.suite, "<worktree-desacoplado-en-" + commit + ">"))
                detalles.append({"unidad": f.unidad, "check": f.check, "firma": f.firma,
                                 "conjunto_causal": grupo, "comando": comando, **parte_publica(f)})
        payload = json.dumps({"lote": tren.id, "B": fase.candidato.B, "C": fase.candidato.C,
                              "pr": entrega.pr, "head": entrega.head, "fallos": detalles}, sort_keys=True)
        efectos.append(Efecto(clave("devolver", tren.id, recibo), "devolver", (tren.id, recibo, payload)))
    return separar_devueltos(estado, tren, fase.candidato.B, culpables, efectos)


def separar_devueltos(estado, tren, base, culpables, efectos):
    """D5 compartido por atribución automática y expulsión por juicio."""
    efectos = list(efectos)
    entradas = {e.entrega.recibo: e for e in estado.entradas}
    sobrevivientes = tuple(r for r in tren.recibos if entradas[r].entrega.pr not in culpables
                           and not isinstance(entradas[r].admision, Rechazado))
    estado = cambiar_fase(estado, tren, Terminal("DEVUELTO"))
    nuevo_id = clave("sobrevivientes", tren.id, sobrevivientes)
    estado = replace(estado, entradas=tuple(
        replace(e, terminal=Terminal("DEVUELTO"), rearme_ci=None, ci_ref_nueva=False)
        if e.entrega.recibo in tren.recibos and
        e.entrega.pr in culpables else replace(e, tren=nuevo_id) if
        e.entrega.recibo in sobrevivientes else e for e in estado.entradas))
    if sobrevivientes:
        nuevo = Tren(nuevo_id, sobrevivientes, Congelado(base))
        estado = programar_armado(estado, nuevo, efectos)
    devueltos = (entradas[r] for r in tren.recibos if entradas[r].entrega.pr in culpables)
    return liberar_particiones(estado, devueltos), tuple(efectos)


def programar_armado(estado, tren, efectos):
    efectos.append(Efecto(clave("armar", tren.id), "armar-candidato", (tren.id,)))
    efectos.extend(efectos_status(tren, "avisar-en-tren"))
    return replace(estado, trenes=estado.trenes + (tren,))


def aviso_conflicto_main(entrega):
    tipo = "avisar-conflicto-main"
    return Efecto(clave(tipo, entrega.recibo), tipo, (entrega.recibo,))


def aplazar_conflicto(estado, tren, conflicto, efectos):
    por_recibo = {e.entrega.recibo: e for e in estado.entradas}
    corte = next((i for i, r in enumerate(tren.recibos)
                  if por_recibo[r].entrega.pr == conflicto.pr), None)
    if corte is None:
        return replace(estado, pausa=Pausa("candidato-no-corresponde")), tuple(efectos)
    anteriores, recibo = tren.recibos[:corte], tren.recibos[corte]
    entrada = por_recibo[recibo]
    base = tren.fase.B
    payload = json.dumps({"lote": tren.id, "pr": conflicto.pr, "archivos": conflicto.archivos,
                          "anteriores": [por_recibo[r].entrega.pr for r in anteriores]})
    tipo = "avisar-conflicto-en-cadena"
    efectos.append(Efecto(clave(tipo, tren.id), tipo, (tren.id, recibo, payload)))
    if not entrada.aplazada_en_base and entrada.admision == Retenido(Motivo.CONFLICTO):
        efectos.append(aviso_conflicto_main(entrada.entrega))
    sobrevivientes = tuple(r for r in anteriores
                           if not isinstance(por_recibo[r].admision, Rechazado))
    nuevo = Tren(clave("sobrevivientes", tren.id, sobrevivientes), sobrevivientes, Congelado(base))
    estado = cambiar_fase(estado, tren, Terminal("REARMAR"))
    estado = replace(estado, entradas=tuple(
        replace(e, tren=nuevo.id) if e.entrega.recibo in sobrevivientes else
        # Contra main (sin prefijo) espera otra base; con prefijo acumula los de esta base.
        replace(e, tren=None, rearmada=True, aplazada_en_base=base, prefijo_en_conflicto=tuple(
            dict.fromkeys((e.prefijo_en_conflicto if e.aplazada_en_base == base else ()) + anteriores))
            if anteriores else ())
        if e.entrega.recibo == recibo else
        replace(e, tren=None, rearmada=True)
        if e.entrega.recibo in tren.recibos else e for e in estado.entradas))
    if sobrevivientes:
        estado = programar_armado(estado, nuevo, efectos)
    return estado, tuple(efectos)


def archivos_acreditados(fase, observacion):
    archivos = observacion.archivos_lote
    if archivos is None:
        return False
    vigentes = {(v.pr, v.head) for v in observacion.prs
                if v.completa and v.abierto and v.base == "main"}
    if any((pr, head) not in vigentes for pr, head, _ in fase.candidato.eslabones):
        return False
    # Una unidad puede incluir el nodo del caso y usar una ruta relativa al paquete.
    rutas = (f.unidad.split("::", 1)[0] for f in fase.fallos)
    return not any(archivo == ruta or archivo.endswith("/" + ruta)
                   for ruta in rutas for archivo in archivos)


def registrar_absoluciones(repo, tren, candidato, absoluciones):
    for absolucion in absoluciones:
        fallo = absolucion.fallo
        payload = json.dumps({
            "repo": repo, "tren": tren.id, "B": candidato.B, "C": candidato.C,
            "unidad": fallo.unidad, "firma": fallo.firma, "caso": absolucion.caso,
            "intentos_C": absolucion.intentos_C, "intentos_B": absolucion.intentos_B,
        }, sort_keys=True)
        yield Efecto(clave("registrar-absolucion", tren.id, fallo.parte, fallo.check, fallo.unidad),
                     "registrar-absolucion", (tren.id, payload))


def avanzar_suite(estado, tren, observacion, despachar=True):
    """Incorpora resultados ligados al intento antes de requerir readmisión."""
    from atribucion import planificar
    fase = tren.fase
    ligada = observacion.tren_id == tren.id
    en_paralelo = observacion.repeticiones_en_paralelo
    if isinstance(fase, Corriendo):
        resultado = observacion.resultado_suite if (ligada and
            observacion.suite_intento == intento_suite(fase) and
            (observacion.suite == fase.suite or
             (intento_suite(fase) or fase.reenganches) and observacion.suite)) else None
        if resultado is not None and fase.esperar_hasta is None:
            fase = replace(fase, suite=observacion.suite)
            if isinstance(resultado, RechazoLanzador):
                return pausar_lanzador(estado, resultado)
            if isinstance(resultado, Incompleto):
                return esperar_infraestructura(estado, tren, fase, resultado, observacion.ahora)
            if not isinstance(resultado, Completo):
                return replace(estado, pausa=Pausa("necesita-juicio", detalle="resultado-desconocido")), ()
            if resultado.veredicto in ("verde", "verde-contra-base"):
                if any(f.clase != "también-base" for f in resultado.fallos):
                    return replace(estado, pausa=Pausa("necesita-juicio", detalle="verde-incoherente")), ()
                c = fase.candidato
                cert = Certificado(c.C, c.arbol, fase.suite, fase.ci, c.B)
                nuevo = cambiar_fase(estado, tren, cert)
                if resultado.veredicto == "verde" and resultado.costo_s > 0:
                    nuevo = replace(nuevo, costo_suite_sana_s=resultado.costo_s)
                return nuevo, ()
            if resultado.veredicto != "rojo" or not resultado.fallos:
                return replace(estado, pausa=Pausa("necesita-juicio", detalle="resultado-desconocido")), ()
            costo_sano = observacion.costo_suite_sana_s or estado.costo_suite_sana_s
            fase = Diagnosticando(fase.candidato, resultado.fallos, fase.suite, fase.ci,
                                   min(3600, 0.5 * costo_sano), restantes=resultado.restantes)
            estado = cambiar_fase(estado, tren, fase)
        elif fase.esperar_hasta is not None:
            if observacion.ahora < fase.esperar_hasta or not despachar:
                return estado, ()
            fase = replace(fase, esperar_hasta=None, reanudaciones=fase.reanudaciones + 1,
                           reenganches=0, registro_reenganche="")
            estado = cambiar_fase(estado, tren, fase)
        if isinstance(fase, Corriendo):
            if (intento_suite(fase) or fase.reenganches) and fase.esperar_hasta is None and despachar:
                efecto = efecto_reanudar_suite(tren, fase)
                return estado, (efecto,)
            return estado, ()
    if fase.pendiente:
        s = fase.pendiente
        ligada = ligada and observacion.reproduccion_id == s.id
        if ligada and observacion.candidato and fase.subconjunto is None and s.objetivo == "subconjunto":
            c = observacion.candidato
            esperados = tuple((pr, head) for pr, head, _ in fase.candidato.eslabones if pr in s.prs)
            if c.B != fase.candidato.B or tuple((pr, head) for pr, head, _ in c.eslabones) != esperados:
                return replace(estado, pausa=Pausa("necesita-juicio", detalle="subconjunto-no-corresponde")), ()
            fase = replace(fase, subconjunto=c)
        if ligada and observacion.conflicto_cadena:
            return replace(estado, pausa=Pausa("necesita-juicio", detalle="conflicto-en-subconjunto")), ()
        resultado = observacion.repeticion if ligada and observacion.suite_intento == intento_suite(fase) else None
        if resultado is not None and fase.esperar_hasta is None:
            if isinstance(resultado, RechazoLanzador):
                return pausar_lanzador(estado, resultado)
            if isinstance(resultado, Incompleto):
                return esperar_infraestructura(estado, tren, fase, resultado, observacion.ahora)
            commit = (fase.candidato.C if s.objetivo == "C" else fase.candidato.B if
                      s.objetivo == "B" else fase.subconjunto.C if fase.subconjunto else "")
            if (not isinstance(resultado, Repeticion) or resultado.sha != commit or
                    resultado.origen_sha != fase.candidato.C or resultado.base != fase.candidato.B):
                return replace(estado, pausa=Pausa("necesita-juicio", detalle="repeticion-no-corresponde")), ()
            if fase.evidencias and resultado.origen_sha256 != fase.evidencias[0].resultado.origen_sha256:
                return replace(estado, pausa=Pausa("necesita-juicio", detalle="origen-distinto")), ()
            fase = replace(fase, evidencias=fase.evidencias + (Evidencia(s, resultado),),
                pendiente=None, subconjunto=None, reanudaciones=0, esperar_hasta=None, reanudar="",
                reintentos_lanzador=0, reenganches=0, registro_reenganche="")
        elif fase.esperar_hasta is not None:
            if observacion.ahora < fase.esperar_hasta or not despachar:
                return cambiar_fase(estado, tren, fase), ()
            costo = costo_atribucion(fase.evidencias, en_paralelo, siguiente=s)
            if costo + fase.costo_infra_s + s.costo_s > fase.presupuesto_s:
                return replace(cambiar_fase(estado, tren, fase), pausa=Pausa(
                    "necesita-juicio", detalle="presupuesto-agotado-en-reanudacion")), ()
            fase = replace(fase, esperar_hasta=None, reanudaciones=fase.reanudaciones + 1,
                           costo_infra_s=fase.costo_infra_s + s.costo_s,
                           reenganches=0, registro_reenganche="")
    if fase.pendiente is None:
        prs = tuple(pr for pr, _, _ in fase.candidato.eslabones)
        presupuesto = fase.presupuesto_s - fase.costo_infra_s
        plan = planificar(fase.fallos, prs, fase.evidencias, presupuesto, en_paralelo=en_paralelo)
        if (plan.incierto.startswith(("confirmacion-candidato:", "confirmacion-base:")) and
                not fase.restantes and len(fase.fallos) <= 2 and
                archivos_acreditados(fase, observacion)):
            automatico = planificar(fase.fallos, prs, fase.evidencias, presupuesto, absolver=True,
                                    firma_distingue_fallo=observacion.firma_distingue_fallo,
                                    en_paralelo=en_paralelo)
            if automatico is not None and not automatico.incierto:
                recientes = dict(observacion.absoluciones_recientes)
                for absolucion in automatico.absoluciones:
                    unidad = absolucion.fallo.unidad
                    if recientes.get(unidad, 0) >= 2:
                        return replace(cambiar_fase(estado, tren, fase), pausa=Pausa(
                            "necesita-juicio", detalle="intermitente-reincidente:" + unidad)), ()
                    recientes[unidad] = recientes.get(unidad, 0) + 1
                plan = automatico
        if plan.incierto:
            return replace(cambiar_fase(estado, tren, fase),
                           pausa=Pausa("necesita-juicio", detalle=plan.incierto)), ()
        if plan.terminado:
            if not plan.causas:
                # Confirmar fallos preexistentes no acredita los checks que el fail-fast no corrió.
                if fase.restantes:
                    return replace(cambiar_fase(estado, tren, fase),
                                   pausa=Pausa("necesita-juicio", detalle="suite-incompleta")), ()
                c = fase.candidato
                return (
                    cambiar_fase(estado, tren, Certificado(c.C, c.arbol, fase.suite, fase.ci, c.B)),
                    tuple(registrar_absoluciones(estado.repo, tren, c, plan.absoluciones)),
                )
            if not despachar:
                return cambiar_fase(estado, tren, fase), ()
            return devolver_atribuidos(estado, tren, fase, plan.causas)
        fase = replace(fase, pendiente=plan.siguiente)
    estado = cambiar_fase(estado, tren, fase)
    if fase.esperar_hasta is not None or not despachar:
        return estado, ()
    return estado, (efecto_diagnostico(tren, fase),)


LIMITE_LOTE = 5


def orden_fifo(entrada):
    return entrada.admitida, entrada.recibida, entrada.entrega.recibo


def siguiente_lote(fila, *, base=None):
    lote = []
    recibos = set()
    incompatibles = set()
    for entrada in fila[:LIMITE_LOTE]:
        recibo = entrada.entrega.recibo
        prefijo = entrada.prefijo_en_conflicto if entrada.aplazada_en_base == base else ()
        choca = recibo in incompatibles or not recibos.isdisjoint(prefijo)
        if entrada.solo:
            # Una entrega `solo` corta la fila aunque hoy no pueda viajar.
            if not lote:
                lote.append(entrada)
            break
        if choca:
            # Sólo esta entrega choca con el lote; las siguientes pueden viajar.
            continue
        lote.append(entrada)
        recibos.add(recibo)
        incompatibles.update(prefijo)
    return tuple(lote)


def paso(estado, observacion):
    """Un paso determinista. Congela a lo sumo un lote y emite sólo datos."""
    if observacion.decision is not None:
        from juicios import resolver
        return resolver(estado, observacion)
    activos = [t for t in estado.trenes if not isinstance(t.fase, Terminal)]
    cert_vencido = None
    if not estado.pausa and activos:
        tren = activos[0]
        if (isinstance(tren.fase, Certificado) and tren.fase.B and observacion.base and
                observacion.base != tren.fase.B and lectura_ci(tren, observacion) != "verde"):
            cert_vencido = tren.id
    if not estado.pausa and activos and isinstance(activos[0].fase, Promoviendo):
        tren = activos[0]
        fase = tren.fase
        if observacion.main is None and not fase.reintento_pendiente:
            # Si main sigue en B, el rojo puede cerrar tras readmitir los heads.
            if lectura_ci(tren, observacion) != "rojo" or observacion.base != fase.certificado.B:
                nuevo, efectos, _ = avanzar_ci_promocion(estado, tren, observacion)
                return nuevo, efectos
        elif observacion.main is not None:
            estado, efectos = aterrizaje(estado, tren, observacion)
            # Sólo EnBase del intento vigente permite readmitir y reintentar.
            lectura = observacion.main
            cert = fase.certificado
            if (estado.pausa or not isinstance(lectura, EnBase) or
                    observacion.tren_id != tren.id or
                    observacion.intento_promocion != fase.intentos or
                    (lectura.B, lectura.C) != (cert.B, cert.C)):
                return estado, efectos
    efectos = []
    entradas = []
    rechazadas = []
    solo_vistas = {}
    vistas = {v.recibo or str(v.pr): v for v in observacion.prs}
    # A landed PR closes as merged; its delivery is settled, not expired.
    terminales = {t.id for t in estado.trenes if isinstance(t.fase, Terminal)}
    # En pausa no se consume ni recupera promover: su push puede haber cerrado
    # los PR. Diferir sólo si falta el resultado; reintento_pendiente guarda
    # que ya se consumió EnBase y no hay un push cuyo aterrizaje sea incierto.
    promociones_pausadas = {t.id for t in activos
                           if estado.pausa and isinstance(t.fase, Promoviendo)
                           and not t.fase.reintento_pendiente}
    esperando_ci = {t.id for t in activos if isinstance(t.fase, EsperandoCI)}
    certificados = {t.id for t in activos if isinstance(t.fase, Certificado)}
    antes_de_ci = {t.id for t in activos if isinstance(t.fase, (Congelado, Armado))}
    con_conflicto = {t.id for t in activos if isinstance(t.fase, Congelado)
                    and observacion.conflicto_cadena and not base_vieja(t.fase, observacion)
                    and observacion.tren_id in ("", t.id)}
    for entrada in estado.entradas:
        entrega = entrada.entrega
        vista = vistas.get(entrega.recibo, vistas.get(str(entrega.pr)))
        if (isinstance(entrada.admision, Rechazado) or
                entrada.tren in terminales | promociones_pausadas):
            entradas.append(entrada)
            continue
        decision = admision_entrada(entrada, vistas, observacion)
        if (entrada.aplazada_en_base and decision == Retenido(Motivo.CONFLICTO)
                and decision != entrada.admision):
            efectos.append(aviso_conflicto_main(entrega))
        if decision == Rechazado(Motivo.HEAD):
            entrada = replace(entrada, admision=Rechazado(Motivo.HEAD), rearme_ci=None,
                              ci_ref_nueva=False)
            efectos.append(Efecto(clave("caduca", entrega.recibo),
                                  "avisar-caducidad", (entrega.recibo,)))
        else:
            if isinstance(decision, Rechazado) and entrada.tren is not None:
                efectos.append(Efecto(clave("caduca", entrega.recibo),
                                      "avisar-caducidad", (entrega.recibo,)))
            if isinstance(decision, Admitido):
                admitida = (entrada.admitida if entrada.admitida is not None
                            else observacion.ahora)
            elif (decision == Retenido(Motivo.INCIERTO) or entrada.partida or
                  entrada.rearmada or entrada.tren in esperando_ci or
                  (decision == Retenido(Motivo.CI) and entrada.tren in certificados) or
                  (cert_vencido is not None and entrada.tren == cert_vencido) or
                  entrada.tren in con_conflicto or
                  (entrada.rearme_ci and entrada.tren in antes_de_ci)):
                # No perder FIFO al retener antes o después de una partición.
                admitida = entrada.admitida
            else:
                admitida = None
            entrada = replace(entrada, admision=decision, admitida=admitida)
            if isinstance(decision, Rechazado):
                entrada = replace(entrada, rearme_ci=None, ci_ref_nueva=False)
            solo_vistas[entrega.recibo] = solo_por_vista(vista)
        if isinstance(decision, Rechazado):
            rechazadas.append(entrada)
        entradas.append(entrada)
    estado = liberar_particiones(replace(estado, entradas=tuple(entradas)), rechazadas)
    entradas = tuple(replace(e, solo=e.partida or solo_vistas[e.entrega.recibo])
                     if e.entrega.recibo in solo_vistas else e for e in estado.entradas)
    estado = replace(estado, entradas=entradas)
    if estado.pausa:
        return estado, tuple(efectos)
    activos = [t for t in estado.trenes if not isinstance(t.fase, Terminal)]
    if activos:
        tren = activos[0]
        miembros = [e for e in entradas if e.entrega.recibo in tren.recibos]
        fase = tren.fase
        if observacion.tren_id and observacion.tren_id != tren.id:
            return estado, tuple(efectos)
        # Un merge por fuera deja a C sin poder aterrizar: no gastar CI ni suite.
        if tren.id == cert_vencido or base_vieja(fase, observacion):
            return rearmar(estado, tren, temprano=True), tuple(efectos)
        ocupada = observacion.rama_ocupada
        if (isinstance(fase, (Armado, EsperandoCI)) and ocupada and
                observacion.tren_id == tren.id and
                ocupada.ref == "refs/heads/tren/" + rama_ci(tren) and
                ocupada.esperado == fase.candidato.C):
            return replace(estado, pausa=Pausa("rama-tren-ocupada")), tuple(efectos)
        if isinstance(fase, Congelado) and observacion.conflicto_cadena:
            return aplazar_conflicto(estado, tren, observacion.conflicto_cadena, efectos)
        if isinstance(fase, Congelado) and observacion.candidato:
            candidato = observacion.candidato
            por_recibo = {e.entrega.recibo: e for e in miembros}
            esperados = tuple((por_recibo[r].entrega.pr, por_recibo[r].entrega.head)
                              for r in tren.recibos)
            if (candidato.B != fase.B or
                    tuple((pr, head) for pr, head, _ in candidato.eslabones) != esperados):
                return replace(estado, pausa=Pausa("candidato-no-corresponde")), tuple(efectos)
            fase = Armado(candidato)
            estado = replace(estado, trenes=tuple(replace(t, fase=fase) if t.id == tren.id
                                                 else t for t in estado.trenes))
        if isinstance(fase, (Corriendo, Diagnosticando)):
            nuevo, suite_efectos = avanzar_suite(estado, tren, observacion,
                all(isinstance(e.admision, Admitido) for e in miembros))
            if any(isinstance(e.admision, Rechazado) for e in miembros):
                nuevo = replace(nuevo, pausa=Pausa("head-cambiado-en-tren"))
            return nuevo, tuple(efectos) + suite_efectos
        if (isinstance(fase, EsperandoCI) and
                not any(isinstance(e.admision, Rechazado) for e in miembros)):
            estado, ci_efectos = avanzar_ci(estado, tren, observacion)
            tren = next(t for t in estado.trenes if t.id == tren.id)
            fase = tren.fase
            if estado.pausa or isinstance(fase, Terminal):
                return estado, tuple(efectos) + ci_efectos
        if (isinstance(fase, EsperandoCI) and observacion.tren_id == tren.id and
                (not fase.solicitud or observacion.ci_recibo == recibo_ci(tren)) and
                observacion.ci_candidato == "verde" and observacion.suite and
                observacion.resultado_suite is not None):
            fase = Corriendo(fase.candidato, observacion.suite, observacion.ci_recibo)
            estado = cambiar_fase(estado, tren, fase)
            nuevo, suite_efectos = avanzar_suite(estado, replace(tren, fase=fase), observacion,
                all(isinstance(e.admision, Admitido) for e in miembros))
            if any(isinstance(e.admision, Rechazado) for e in miembros):
                nuevo = replace(nuevo, pausa=Pausa("head-cambiado-en-tren"))
            return nuevo, tuple(efectos) + suite_efectos
        # Los resultados completados quedan incorporados antes de esta puerta.
        if any(isinstance(e.admision, Rechazado) for e in miembros):
            return replace(estado, pausa=Pausa("head-cambiado-en-tren")), tuple(efectos)
        cert = certificado_de(fase)
        if cert is not None and cert.B:
            estado, ci_efectos, verde = avanzar_ci_promocion(estado, tren, observacion)
            if not verde:
                return estado, tuple(efectos) + ci_efectos
            tren = next(t for t in estado.trenes if t.id == tren.id)
            fase = tren.fase
        if any(not isinstance(e.admision, Admitido) for e in miembros):
            return estado, tuple(efectos)
        if isinstance(tren.fase, Congelado) and isinstance(fase, Armado):
            return estado, tuple(efectos)
        if isinstance(fase, Certificado):
            nuevo, promocion = aterrizaje(estado, tren, observacion)
            return nuevo, tuple(efectos) + promocion
        if isinstance(fase, Promoviendo) and fase.reintento_pendiente:
            if fase.intentos >= LIMITE_PROMOCION and not fase.reintento_autorizado:
                return replace(estado, pausa=Pausa("promocion-rechazada")), tuple(efectos)
            fase = replace(fase, intentos=fase.intentos + 1, reintento_pendiente=False,
                           reintento_autorizado=False)
            efectos.append(efecto_promover(tren.id, fase.intentos))
        elif isinstance(fase, Armado):
            # C puede ser idéntico al del tren anterior. Pedir evidencia de esta ref.
            solicitud = int(any(e.rearme_ci or e.ci_ref_nueva for e in miembros))
            fase = EsperandoCI(fase.candidato, observacion.ahora, solicitud)
            clave_ci = clave("ci", tren.id, solicitud) if solicitud else clave("ci", tren.id)
            efectos.append(Efecto(clave_ci, "solicitar-ci", (tren.id,)))
        elif (isinstance(fase, EsperandoCI) and observacion.ci_candidato == "verde" and
              (not fase.solicitud or (observacion.tren_id == tren.id and
                                     observacion.ci_recibo == recibo_ci(tren)))):
            if observacion.resultado_suite is None:
                efectos.append(Efecto(clave("suite", tren.id), "iniciar-suite", (tren.id,)))
            if observacion.suite:
                fase = Corriendo(fase.candidato, observacion.suite, observacion.ci_recibo)
                estado = cambiar_fase(estado, tren, fase)
                nuevo, suite_efectos = avanzar_suite(estado, replace(tren, fase=fase), observacion)
                return nuevo, tuple(efectos) + suite_efectos
        estado = replace(estado, trenes=tuple(replace(t, fase=fase) if t.id == tren.id
                                             else t for t in estado.trenes))
        return estado, tuple(efectos)
    # Sin tren activo, el aplazado con prefijo recupera su lugar FIFO.
    fila = sorted((e for e in entradas if isinstance(e.admision, Admitido) and e.tren is None
                   and (e.aplazada_en_base != observacion.base or e.prefijo_en_conflicto)),
                  key=orden_fifo)
    lote = siguiente_lote(fila, base=observacion.base)
    if not lote or not observacion.base:
        return estado, tuple(efectos)
    recibos = tuple(e.entrega.recibo for e in lote)
    tren_id = clave("tren", estado.repo, observacion.base, recibos)
    generacion = 0
    usados = {t.id for t in estado.trenes}
    while tren_id in usados:
        generacion += 1
        tren_id = clave("tren", estado.repo, observacion.base, recibos, generacion)
    tren = Tren(tren_id, recibos, Congelado(observacion.base))
    estado = replace(estado, entradas=tuple(replace(e, tren=tren_id, rearmada=False)
                     if e.entrega.recibo in recibos else e for e in entradas))
    estado = programar_armado(estado, tren, efectos)
    return estado, tuple(efectos)


# Tagged encoding keeps the SQLite/event contract explicit and round-trippable.
_TIPOS = {c.__name__: c for c in (Entrega, Admitido, Retenido, Rechazado, Entrada,
          Pausa, Decision, Candidato, ConflictoCadena, RamaOcupada, Aterrizado, EnBase, EscritorExterno,
          Incierto, EsperaPrevia, BloqueoPrevia, VentanaAutorizada, Fallo, Completo, Incompleto, RechazoLanzador, Atribucion, Congelado,
          Armado, EsperandoCI, Corriendo, Certificado, Diagnosticando,
          Promoviendo, Terminal, Tren, Estado, Efecto, Intento, UnidadRepetida,
          Repeticion, Reproduccion, Evidencia, Causa, ResultadoCI)}


def datos(valor):
    from dataclasses import fields, is_dataclass
    if is_dataclass(valor):
        return dict({"_tipo": type(valor).__name__},
                    **{f.name: datos(getattr(valor, f.name)) for f in fields(valor)
                       # Conservar payloads históricos de sombra/custodia.
                       if not (isinstance(valor, Entrada) and f.name in (
                                   "partida", "rearmada", "particion", "rearme_ci",
                                   "ci_ref_nueva", "aplazada_en_base", "prefijo_en_conflicto")
                               and not getattr(valor, f.name))
                       and not (isinstance(valor, VistaPR) and f.name == "ventana"
                                and valor.ventana is None)
                       and not (isinstance(valor, Certificado) and f.name == "ci_pendiente_desde"
                                and valor.ci_pendiente_desde is None)
                       and not (isinstance(valor, Promoviendo) and f.name == "reintento_autorizado"
                                and not valor.reintento_autorizado)})
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
