# Fleet local — SEN-3820 r2

CLI opt-in de un slot FIFO por autoridad local, Python 3.9/3.14 por inspección,
biblioteca estándar, Linux/macOS POSIX. Esta entrega contiene código y pruebas
**escritas, cero ejecutadas**. MacBook sólo permite edición e inspección.

El contrato público es `fleet-job-api@1`; el ledger usa esquema **4**, incompatible
con 2/3, sin migración. El código no activa hostgate, reservas compartidas, Mini,
desk, SSH, Windows, cancelación ni selección de nodos. Los contratos vigentes
permanecen intactos. [Contrato y procedencia](SEN-3820.md) y
[matriz completa de pruebas pendientes](tests/COVERAGE.md).
[Respuesta r2 y regresiones contra0b](SEN-3820-r2.md).

## Interfaz

Ejemplos documentales para un futuro entorno autorizado; **NO EJECUTAR aquí**.
El estado debe ser propio, privado 0700, físico, exclusivo y sobre almacenamiento
local con locks/durabilidad adecuados. No usar otro state-dir para eludir retención.

```text
fleet/bin/fleet --state-dir /estado/propio submit --client qa --cwd /proyecto/propio --idempotency-key suite-3820 --artifact reports/result.json -- tool check
fleet/bin/fleet --state-dir /estado/propio status --idempotency-key suite-3820
fleet/bin/fleet --state-dir /estado/propio wait --idempotency-key suite-3820 --timeout 30
fleet/bin/fleet --state-dir /estado/propio evidence j_<32hex> --attempt a_<32hex>
fleet/bin/fleet --state-dir /estado/propio recover
```

`--state-dir` puede ir antes o después del subcomando. Precedencia: argumento,
`FLEET_STATE_DIR`, `${XDG_STATE_HOME:-$HOME/.local/state}/fleet-pilot`. Se rechazan
symlinks en el recorrido físico y estados públicos/ajenos o rutas reservadas
`pao`, `mini-carga`, `u3` bajo raíces de estado. No se cambian sus permisos.
`--client` > `FLEET_CLIENT` > usuario@host es etiqueta; no otorga autoridad.

`submit` acepta argv literal, stdin cerrado, cwd y entorno completos del emisor.
Captura los archivos admitidos antes de vincular la clave y ejecuta una copia
materializada; no ejecuta directamente el origen. `--input`, `--private`,
`--artifact`, `--credential-env` son repetibles. PWD presente se transforma al cwd
materializado. Las dependencias externas y los ejecutables no quedan congelados.
El comando debe ser compatible: no daemonizar, cambiar sesión o escapar del grupo
original. No existe aislamiento de seguridad ni prueba global de procesos escapados.

La clave opcional es ASCII `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`, ámbito
`(authority UUID, uid OS, key)`. Misma clave y contenido efectivo privado igual
reproduce IDs/admisión originales; distinto devuelve conflicto5/existing original.
La comparación incluye manifiesto, bytes reales, HEAD, cwd relativo, argv, entorno,
client, selecciones y declaraciones/políticas. No usa sólo hashes o redacción.
PWD derivable es la excepción; si se declara credencial, también cuenta su valor raw.
Punto no elimina otras selecciones: `--input . --input foo` difiere de `--input .`
aunque hoy capture los mismos bytes. Se validan todas las selecciones explícitas;
sólo se ordenan/eliminan duplicados idénticos y se equiparan automático/punto solo.
Un replay no despacha, renueva, repara, libera ni reabre. Sin clave, cada envío es nuevo.

`submit0` sólo confirma admisión histórica. Se imprime después de intentar el
despacho opcional, sin prometer readiness: `wait` posterior puede dar3 transitorio.
Incluso un lanzamiento de supervisor fallido conserva la admisión y produce su
expediente original never-created. `--enqueue-only` omite la solicitud inmediata;
un supervisor ya activo puede reclamar ese FIFO si el control está abierto.

Status/wait/evidence exigen exactamente JOB o `--idempotency-key`; `--attempt`
opcional debe coincidir. No necesitan origen ni entorno del envío. La admisión es
inmutable y va separada de `observation`. Not-found sólo describe una revisión de
esa autoridad. Captura candidata rechazada dice `candidate.accepted=false` y
`key_binding=not_evaluated`; no afirma que una clave histórica esté libre.

Cada respuesta operativa es un JSON de una línea, con `api`, `operation`, autoridad
cuando acreditada y errores `{code,effects}`. No publica argv/entorno/comparador.
`--help` es ayuda humana. Salida perdida o muerte preentrypoint puede impedir JSON.

| Código | Significado |
| --- | --- |
| 0 | Submit: admisión; status/evidence: lectura; wait: complete+succeeded; recover/operador: decisión confirmada, no éxito global de jobs. |
| 1 | Sólo wait: complete+failed (exit positivo o never-created íntegro). |
| 2 | Uso, candidato, selector, estado ausente/ilegible/incompatible, autenticación o prueba inválida. |
| 3 | Wait: incertidumbre/incomplete inmediato; recover parcial/retención; operador insuficiente/stale/bloqueado o persistencia incierta; submit no decidible. |
| 4 | Sólo wait pending: deadline agotado. |
| 5 | Conflicto de contenido/operation-id o generación/época equivocada. |
| 130 | SIGINT manejado del cliente; consultar lo durable, no inferir cancelación ni rollback. |

Los lectores nunca escriben el estado original. Copian BD/WAL bajo lock a un temporal
privado independiente de TMPDIR/TEMP/TMP y consultan la copia. Recover sí es un
escritor explícito: reconcilia publicación/release ordinarios acreditados y reclama
sólo el pendiente FIFO seguro. No adopta procesos, repone permisos, cancela, reintenta
ni continúa operaciones privilegiadas. Ver la tabla total en SEN-3820.md.
Error de apertura/lock/consulta/selector en status/evidence/wait/operator.status da
2/read_only, sin observation inventada. Wait3 sigue reservado a incertidumbre o
completion incompleta observadas;4 al deadline pending;130 a SIGINT manejado.
Un escritor con timeout de lock conserva3/unknown.

## Retención y operación local

Colección incompleta conserva el slot por defecto y puede inmovilizar toda la
autoridad indefinidamente. Finished, exit0, grupo vacío, TTL y salud no autorizan
release. Hace falta cierre original positivo, permiso cerrado y publicación/colección
íntegras para liberación ordinaria. Una señal conserva `signal`, outcome incomplete
(y wait3), aunque con expediente íntegro sí puede liberar capacidad.

Existe salida explícita **sólo pilot_slot**, para capacidad con cierre original ya
acreditado aunque falte evidencia del job. Invocaciones documentales:

```text
fleet/bin/fleet --state-dir /estado/propio operator drain --operation-id drain-1 --reason "investigación local"
fleet/bin/fleet --state-dir /estado/propio operator assess j_<32hex> --attempt a_<32hex> --reservation r_<32hex> --generation 1 --drain-id d_<32hex> --operation-id assess-1 --reason "verificar cierre original"
fleet/bin/fleet --state-dir /estado/propio operator release j_<32hex> --attempt a_<32hex> --reservation r_<32hex> --generation 1 --drain-id d_<32hex> --proof p_<32hex> --operation-id release-1 --reason "resolver capacidad retenida"
fleet/bin/fleet --state-dir /estado/propio operator status --operation-id release-1
fleet/bin/fleet --state-dir /estado/propio operator reopen --drain-id d_<32hex> --operation-id reopen-1 --reason "capacidad reconciliada"
```

Actor nativo OS/owner del estado; no se acepta `--actor`, `--uid`, force ni JSON de
cierre. Motivo UTF-8 de1–512 caracteres sin controles ni secretos declarados.
Drain impide nuevos claims, también el relevo ordinario del supervisor, sin cancelar
carga existente. Assess conserva una prueba ligada al cierre original, época y
revisión relevante. Release revalida y confirma capacidad+decisión+auditoría en la
misma transacción; mantiene drained, colección e historia incompletas.
Reopen requiere reconciliación positiva y época vigente; no despacha por sí mismo.
Después se solicita recover. `already_drained` da0, `not_drained` da3 y época vieja5.

Decisión, binding y auditoría locales son atómicos: status muestra not_found o
committed; **no hay intención pending durable**. Rollback no deja binding; ACK perdido
se resuelve por status/repetición exacta. Otra petición con igual operation-id da5.
Recover devuelve `operator_pending=[]`; nunca completa una acción del operador.
Un conflicto conserva en auditoría `request` con los diez campos canónicos de la
petición denegada, incluidos action/backend/selectores/drain/proof/nulls, saneados
con credenciales persistidas. No cambia el binding/receipt original. Backend
operatorio tiene cota de forma1–128 caracteres sin controles/surrogates; no añade
soporte a ningún backend.

`operator status --after-seq N --after-audit-seq A --limit 50` pagina decisiones y
auditoría con cursores independientes. Empezar con N=A=0; después pasar
`next_after_seq` y `next_after_audit_seq` hasta `has_more=false`. Cada lista va en
orden seq y tiene como máximo limit entradas (1–100, default50). Un stream vacío
conserva su cursor; has_more usa ambos. Si se omite el cursor audit, su offset
inicial es N por compatibilidad; para continuar deben enviarse ambos cursores.
El fin describe esa revisión; cada página tiene su copia, sin snapshot concurrente
de todo el recorrido. Pueden conservarse ambos offsets para otra consulta posterior.
Con `--operation-id` se devuelve únicamente el recibo exacto y el control actual.

`--backend host-reservation@1` se rechaza explícitamente en submit y operador.
**C21 positivo permanece abierto; O10 positivo está fuera de SEN-3820.** Estos
rechazos no son integración, simulación ni prueba positiva de hostgate.

## Persistencia, privacidad y límites

BD WAL/FULL, lock de directorio y transacciones serializan claves/claim/control/audit.
Un job, un intento, un permiso durable no regenerable. Se retienen claves, cápsulas,
historia, pruebas y auditoría sin TTL/purga. Preparaciones fallidas o huérfanas quedan
atribuidas y retenidas; sólo su creador puede retirar una preparación resuelta y no
referenciada. Recover/operador no limpian preparaciones.

Cápsulas, comparador, argv/entorno y auditoría son plaintext privados 0700/0600,
sin defensa frente a root o adversario del mismo uid. Los payloads normalizados
pueden ser0644/0755 dentro de directorios privados. Privados por defecto/ampliables
se excluyen de captura/huella. Secretos conocidos/declarados se rechazan en entradas
admitidas y se redactan en streams; secretos no declarados en archivos ordinarios
pueden viajar. No existe detector universal ni garantía sobre transformaciones.

Límites:20.000 archivos,32MiB/archivo,256MiB de entrada,1MiB de metadata comparable
y1MiB de argv/entorno,256MiB/stream; copia lectora de BD/WAL limitada a256MiB por
archivo. Exceso/fallo no purga asociaciones ni concede permisos. La certificación de
filesystem, semántica nativa de Linux/Darwin y durabilidad siguen pendientes runtime.

## Suite futura — NO EJECUTAR en MacBook

PAO prepara una copia nueva del head congelado y el entorno propio, sólo después de
reviews estáticas nuevas, detrás de SEN-3821 101×2 y en su turno autorizado Mini.
Esta entrega no prepara copia, payload, launcher ni FIFO.

Con `FLEET_TEST_RUN_BASE` físico, propio0700 dentro de la futura corrida, repetir con
cada intérprete real provisto (3.9 y3.14), sin instalar ni sustituir interpretes:

```sh
PYTHONDONTWRITEBYTECODE=1 /ruta/python3.9 -B -m unittest discover -s fleet/tests -p 'test_cli.py' -v
PYTHONDONTWRITEBYTECODE=1 /ruta/python3.14 -B -m unittest discover -s fleet/tests -p 'test_cli.py' -v
```

Un único TestCase hereda mixins; discovery sólo `test_cli.py` evita duplicación.
Inventario textual: los53 métodos r1 se conservan;16 base +27 core +15 operator
=**58 métodos**, cinco nuevos r2 y C02 reforzado;
34 filas contractuales (26 definidas,6 parciales/condicionales,2 positivas abiertas)
y3 requisitos adicionales. Método y subcaso son unidades diferentes.
La matriz detalla precondiciones faltantes; su ausencia falla explícitamente, sin
skip contado como cobertura. El harness usa CLI real, recursos propios, barreras
con watchdog30s y contadores externos; retiene estados incompletos o no atribuibles.
La fixture externa de contención de directorio r2 tiene watchdog60s y gate propio.
Guarda snapshots, campañas, contadores y copia verificada de evidencia pública antes
de retirar fixtures completas en `evidence-<fixture>/public-snapshots.json`.
No hay imports Fleet, SQL, hooks privados ni respuestas simuladas en los tests.
