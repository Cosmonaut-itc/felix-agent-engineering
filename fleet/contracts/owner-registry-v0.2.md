# Registro de propietario Fleet para U1, revisión documental preestable r4

Revisión documental preestable de 2026-10-24, para revisión documental r4. El esquema se mantiene literalmente como `fleet-owner-registry@0.2`. Esta revisión aclara decisiones para SEN-3917; no cambia el protocolo activo ni activa productor o consumidor. Conserva la corrección de los espacios de `lstart` introducida en v0.1. Fleet define el contrato; Mini implementa con comprador/U1. La adopción requiere coordinación con U1. No declara APTO ni estable. No sustituye `fleet/host-reservation@1`.

## Propietario, ubicación y publicación

El supervisor local de Fleet es el único escritor de su registro, independiente del cliente. Escritor y lector resuelven la ubicación fija `~/.local/state/fleet/owner-registry` desde el home de la cuenta de su uid operativo. En la Mini esa cuenta es cosmonaut. Se consulta el directorio de cuenta del sistema, como `pwd.getpwuid(os.getuid()).pw_dir`; no se acepta `HOME` heredado, otra variable de entorno, flag ni ruta del cliente. Un parámetro de función puede cambiar la raíz sólo en pruebas. El escritor usa exactamente la misma regla que U1.

Los registros no cerrados ocupan `<ownerRegistryDir>/runs/<runId>/owner.json`. Los cerrados acreditados se archivan en `<ownerRegistryDir>/closed/<runId>/owner.json`. U1 enumera únicamente `runs/`, nunca `closed/`. Esta ubicación por home de cuenta para el registro U1 prevalece sobre la referencia genérica a configuración de host en `fleet/host-reservation@1`; no cambia la ubicación ni la configuración del almacén hostgate. `runId` cumple `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`. No admite punto inicial, puntos en otra posición, separadores, escapes ni segmentos relativos. El nombre `.next` queda prohibido. Los nombres temporales son archivos regulares, nunca subdirectorios adicionales.

La raíz y sus directorios son privados del uid operativo, modo 0700, sin componentes symlink. Los registros son archivos regulares propios 0600. El escritor publica mediante temporal exclusivo en el mismo directorio, escritura completa, fsync del archivo, rename atómico y fsync del directorio. Revalida reserva y generación vigentes antes de publicar. Un dueño anterior no reemplaza el registro vigente.

PAO comunicó la comprobación de Mini de que esta ubicación queda fuera de los globs del limpiador instalado y del revisado. La regla 7 recorre HOME y sólo borra directorios `.next`; la gramática anterior evita ese nombre. La posible poda de `$HOME/.local/state` en una ronda futura del limpiador no es una precondición de este borrador. Evidencia declarada por Mini en `~/.claude/encargos/mini-carga/sen3917/verificaciones-host/`.

U1 sólo lee. No crea, repara, migra, renueva, borra ni escribe estos archivos. `limpieza-disco` tampoco toca este directorio.

## Forma de un registro

El límite por archivo es 65 536 bytes de JSON UTF-8. Se rechazan claves duplicadas, números no finitos, campos desconocidos y formas incompatibles con la versión. Los enteros JSON se preservan exactamente; un consumidor que no pueda conservar su precisión rechaza esta versión. No se impone una cota adicional de `2^53−1`, y `uniqueId` sigue siendo un string decimal uint64. Los identificadores son strings ASCII no vacíos de hasta 128 bytes, salvo restricciones más precisas abajo. PID, PGID y SID son enteros positivos; uid y secuencias son enteros no negativos. Los booleanos no son enteros válidos. No contiene argv, entorno, secretos ni rutas privadas de entradas.

| Campo | Forma y condición |
|---|---|
| schema | Literal `fleet-owner-registry@0.2`. U1 sólo acepta versiones acordadas explícitamente. |
| runId | Es exactamente el id de la corrida de remote-run en `<homeCuenta>/remote-run/runs/<runId>`, asociado por el escritor a ese trabajo, intento y reserva. `homeCuenta` es el home de la cuenta operativa definido arriba. El mismo id nombra el directorio del registro; cumplir la gramática por sí solo no acredita esa asociación. Nunca es un id de turno ni una ruta elegida por el cliente. |
| jobId, attemptId | Identificadores propios del trabajo e intento. No son ids de un turno. |
| reservationId, generation | Reserva host-side y generación positiva. Sólo identifican, no conceden autoridad. |
| physicalHostId, executorId | Informativos para U1: se publican, pero U1 no los compara ni exige configuración de nodo. Windows y WSL comparten physicalHostId en Fleet. Este borrador sólo define procesos Unix y no habilita desk. |
| namespaceId | Esta versión sólo soporta Darwin. Usa `{encoding: "darwin-host@1", value: <kern.uuid>}`, con UUID canónico en minúsculas. U1 lo informa y no lo compara. La identidad Darwin la fijan bootSession y uniqueId. Linux queda fuera de `fleet-owner-registry@0.2`. En un host Darwin, un encoding Linux en el registro es error de forma: invalida el censo completo del ciclo con aviso `observador-no-soportado`; no es una corrida válida con observador indisponible. No se afirma soporte de WSL. Linux requiere una versión y aceptación propias. |
| bootId | En Darwin, `{encoding: "darwin-bootsessionuuid@1", value: <UUID>}`. UUID en minúsculas con guiones. |
| principal | `{uid: <entero no negativo>}`. U1 exige `principal.uid == st_uid del archivo == uid del lector`. Si no coincide, el registro es inseguro y produce sin dueño con aviso. El escritor aplica la misma cuenta operativa. |
| supervisor | Identidad de proceso definida abajo, observada por Fleet. Nunca proviene del estado de un turno. |
| commandRoot | Identidad de proceso o null antes de un spawn confirmado o si el escritor no acredita su vínculo nativo con el lanzamiento. Null no prueba never-created. Es la identidad nativa del proceso `runner.sh` cuyo PID está en `<homeCuenta>/remote-run/runs/<runId>/pid` y que se acepta tras `started`. Ese directorio remoto, junto con su marcador `started`, es la fuente del puente Mini y está separado de `<ownerRegistryDir>/runs/<runId>`; `runId` es el mismo id de corrida en el puente. El runner escribe `echo $$ > pid` antes del marcador `started`. `cmd.sh` y el guardián de SEN-3915 anterior a `started` no son `commandRoot`; la ruta identifica la fuente y no añade un campo al esquema. |
| scope | `{kind: "unix-session-pgid-lineage@1", sid, pgid}`, o null si no se confirma. Un PGID o PID aislado no acredita árbol vacío. Con este scope, `commandRoot` debe ser no nulo y `scope.sid == scope.pgid == commandRoot.pid`; esta igualdad publicada es una regla de forma, no observación del liderazgo. |
| phase | `starting`, `running`, `closing`, `uncertain` o `closed`. U1 contrasta este estado con observación. No lo toma como autorización. |
| heartbeatSequence | Entero no negativo creciente del supervisor. U1 sólo lo usa para avisos, nunca para atribuir o declarar cierre. |
| heartbeatIntervalSec | Campo opcional, entero positivo, nunca booleano. Si falta, U1 usa 900 segundos para el aviso de frescura. `null` es malformado. |
| startedUtc, updatedUtc | Instantes UTC con forma `YYYY-MM-DDTHH:MM:SSZ` o `YYYY-MM-DDTHH:MM:SS.<1 a 6 dígitos>Z`. U1 valida y compara los instantes parseados, nunca el texto. Tiempos inválidos o más de seis dígitos fraccionarios hacen inválida la frontera. Sólo indican cronología y frescura; nunca sustituyen el nacimiento del proceso. |
| endedUtc, closure | Ambos null o ambos presentes. Sólo pueden estar presentes con `phase="closed"`. El objeto closure tiene los campos exactos y concordancias de la sección de cierre. U1 no libera reservas a partir del marcador. |

## Identidad Unix de proceso

Cada identidad incluye `pid`, `start`, `birth` y `birthSecondary`. El host, namespace y boot son los del registro.

`start` tiene la forma `{encoding: "ps-lstart-utc@1", value: <cadena canónica>}`. Productor y consumidor capturan `ps -p PID -o lstart=` con **`LC_ALL=C TZ=UTC0`** y normalizan con **`' '.join(s.split())`**. El resultado contiene exactamente cinco tokens unidos por un único espacio. Sólo se aceptan nombres de día y mes de locale C, fecha válida y hora UTC. No se compara la salida cruda.

Ejemplo de día de un dígito:

```text
captura:  "Wed Nov  4 19:25:34 2026    \n"
canónica: "Wed Nov 4 19:25:34 2026"
```

La misma regla se aplica si U1 obtiene los cinco tokens desde `ps -axo`. La Mini observó cuatro espacios de relleno al final de una captura real el 2026-10-24; la canonicalización los elimina. No se usa hora local ni `time.mktime`. Esta versión elige cadena UTC, no epoch. SEN-3888 corrige primero ambos caminos de U1 en el PR que implemente el consumidor.

En Darwin, la identidad primaria `birth` es:

```json
{
	"encoding": "darwin-uniqueid@1",
	"bootSession": "00000000-0000-0000-0000-000000000001",
	"uniqueId": "1234567890123456789"
}
```

`bootSession` es `sysctl -n kern.bootsessionuuid`, normalizado como UUID en minúsculas y coincidente con `bootId.value`. `uniqueId` es `p_uniqueid` de `PROC_PIDUNIQIDENTIFIERINFO`, representado como string decimal canónico positivo de hasta 20 dígitos, dentro de uint64. Así conserva precisión en consumidores JSON. `p_puniqueid`, `p_uuid` y `p_idversion` no son necesarios para esta igualdad y no se publican en v0.2.

Comprador verificó estas lecturas sin sudo en MacBook. Mini las repitió en macOS 27.0 para procesos propios y de otro uid. Que `p_uniqueid` no se reutiliza dentro de un boot es una inferencia de la documentación de XNU, no una prueba runtime de reutilización de PID. La implementación conserva esa distinción en su informe.

`birthSecondary` en Darwin es null o `{encoding: "darwin-libproc-start@1", seconds: <string decimal>, microseconds: <0..999999>}` de `PROC_PIDTBSDINFO`. Sólo se exige su captura cuando el uid coincide y el observador la soporta. Mini comprobó que el segundo coincide con `lstart` UTC y que otro uid devuelve EPERM. Cuando no está disponible, se publica `null`; la causa `EPERM` aparece sólo en el informe externo. U1 valida la forma si el valor está presente, pero no lo usa para atribuir ni sustituye la identidad primaria. Si la identidad primaria y las demás comprobaciones son verificables, la ausencia de esta identidad secundaria opcional no invalida por sí sola la comparación.

En Darwin U1 compara PID, cadena UTC canónica, `birth.bootSession`, `birth.uniqueId` y su concordancia con `bootId`. `namespaceId`, `physicalHostId` y `executorId` son informativos y no entran en esa igualdad. `lstart` tiene resolución de segundos y no reemplaza el nacimiento nativo. La identidad se revalida cuando se atribuye el proceso. Ninguna identidad del registro autoriza señales ni concede propiedad sobre procesos de otro principal.

## Lectura U1 y límites por ciclo

U1 primero valida el censo íntegro: formas, tipos, límites y todas las concordancias internas de cada registro. Sólo si la frontera completa es íntegra calcula por corrida los resultados según sus propias observaciones. Una discrepancia con la observación (por ejemplo, liderazgo observado distinto) no vuelve malformado un registro internamente coherente. U1 atribuye en sombra, sin cambiar protección, lista de procesos a terminar ni política de señales. Para un registro válido produce cuatro resultados. Con `closed` se aplica primero la regla 1. Fuera de `closed`, antes de comprobar reutilización, un `bootId.value` distinto del boot observado produce no confirmado con causa `otro-boot`; si el boot no se puede verificar, produce no confirmado con causa `boot-no-verificable`. Sólo con boot coincidente continúa con las reglas 2 a 4:

1. **Cierre acreditado de ámbito o no confirmado.** Con fase `closed`, U1 evalúa sólo las concordancias y la evidencia del cierre de la sección correspondiente. No exige identidades de proceso vivas. `unix-scope-empty` puede acreditar que el ámbito observado está vacío, no que terminó toda la carga ni sus posibles escapes. El cierre informativo no libera una reserva.
2. **PID reutilizado.** Fuera de `closed`, si cualquier identidad requerida no nula tiene un PID existente con `lstart` o `uniqueId` distinto, U1 devuelve PID reutilizado. Nunca hereda dueño ni autoriza acciones.
3. **No confirmado, con causa.** Fuera de `closed`, tras descartar identidad reutilizada, si una identidad requerida está ausente o no se puede verificar, `commandRoot` es null o el liderazgo no se confirma según la sección de cierre, U1 devuelve no confirmado. Si `commandRoot` terminó y la fase no es `closed`, la causa es `raíz terminada`, aunque el supervisor siga vivo.
4. **Dueño vivo.** Fuera de `closed`, sólo si todas las identidades requeridas coinciden y el liderazgo observado concuerda con `scope`. Un supervisor vivo no ampara una raíz terminada.

Registro ausente, ilegible, corrupto, malformado, inseguro o versión desconocida produce **sin dueño con aviso tipado**. La validación completa de la frontera incluye el parseo de las fechas y, como única concordancia cronológica, `startedUtc <= observedUtc <= endedUtc` cuando hay cierre; incluye además reserva/generación, nulidades y concordancias publicadas entre `scope`, `commandRoot` y `closure`; cualquier error de forma, incluso en un registro `closed`, invalida el censo Fleet completo del ciclo. También lo invalida un directorio de corrida enumerado sin `owner.json`, o una corrida/archivo que desaparece tras enumerarlo; ambos casos generan aviso tipado. Si la raíz de registro o `runs/` falta o es insegura, si cualquier archivo de registro es ilegible, inseguro o de versión desconocida, o si se excede cualquier límite por archivo o ciclo, el censo Fleet completo de ese ciclo queda sin dueño. U1 no publica atribuciones parciales. Sólo después de validar el censo íntegro calcula los cuatro resultados por corrida en el orden anterior y aplica las comprobaciones de la sección de cierre. Las discrepancias frente a observaciones se clasifican por esas reglas y no vuelven malformado un registro internamente coherente. La observación ordinaria de procesos de U1 se conserva. U1 no repara el registro ni recurre a heurísticas de turno. Estos fallos no significan cero carga ni capacidad libre.

Por ciclo, U1 examina exclusivamente `runs/`, con un máximo de **4096 entradas de corrida no archivadas** y lee como máximo **8 388 608 bytes agregados** de registros. El límite individual sigue en 65 536 bytes. Los límites cubren entradas inválidas e intentos fallidos de lectura; no sólo registros válidos. La enumeración es incremental y sin recorrido recursivo. Una entrada adicional o un byte adicional sólo sirven de centinela para detectar exceso y no se procesan como registro.

Al exceder cualquiera de los límites, incluido el límite individual de 65 536 bytes, el ciclo del registro produce **sin dueño con aviso `registry-limit-exceeded`**, con el límite alcanzado. Descarta atribuciones parciales de ese ciclo para no publicar un subconjunto como censo completo. U1 conserva su observación ordinaria de procesos y su política conservadora ante duda. No presenta el exceso como lista vacía saludable. La implementación debe acotar también la enumeración y la memoria; no puede cargar todo el directorio o un archivo ilimitado antes de comprobar los topes.

Una raíz finalizada con descendientes posibles sigue `closing` o `uncertain`. Un latido vencido, SSH caído, PID reutilizado, otro boot, archivo eliminado o `closed` sin cierre válido no acreditan fin de la carga.

## Frescura y avisos

`updatedUtc` y `heartbeatSequence` sólo generan avisos. No acreditan ni invalidan por sí solos la atribución, el cierre, la capacidad o el permiso de señal. Una identidad nativa verificable conserva su resultado aunque exista aviso de latido. Un `updatedUtc` anterior a `startedUtc`, desordenado respecto de otras actualizaciones o futuro sólo genera aviso; no añade otra concordancia cronológica de frontera.

Con `heartbeatIntervalSec`, el umbral es `max(300, 3 * heartbeatIntervalSec)` segundos. Sin ese campo opcional, es 900 segundos. U1 avisa si la edad de `updatedUtc` supera ese umbral. La falta de avance de la secuencia durante ese plazo, un retroceso o un timestamp futuro generan avisos con causa. La frescura sólo genera avisos y no cambia el resultado de atribución. El seguimiento necesario para avisar no escribe el registro Fleet. El umbral nunca se interpreta como TTL de propiedad.

## Cierre y concordancias

`closure` es null o un objeto con el conjunto exacto de campos según kind:

```text
never-created:    {kind, reservationId, generation, observedUtc, observer}
unix-scope-empty: {kind, reservationId, generation, sid, pgid, observedUtc, observer}
```

`kind` es el literal de la variante. `reservationId` es un identificador string y `generation` un entero positivo, iguales a los del registro. `observedUtc` usa el mismo formato UTC y reglas de parseo que `startedUtc` y `updatedUtc`. `observer` es exactamente `fleet-supervisor@1` en esta versión. Otra autoridad requiere acuerdo de una versión posterior. El valor no autentica por sí mismo al observador. SID y PGID son enteros positivos, nunca booleanos.

U1 valida todas estas concordancias:

- En `unix-scope-empty`, `scope` no es null, `commandRoot` no es null, y sid y pgid coinciden exactamente con `scope.sid`, `scope.pgid` y `commandRoot.pid`.
- En `never-created`, `commandRoot` y `scope` son null.
- `startedUtc <= observedUtc <= endedUtc`.
- `closure` y `endedUtc` son ambos null o ambos presentes. Sólo están presentes con `phase="closed"`; `closed` exige ambos presentes.

El escritor publica `scope` sólo tras observar en la raíz viva `getsid(pid) == getpgid(pid) == pid`, y verificar la identidad `pid/start/boot/uniqueId` antes y después de observar el liderazgo y después de `started`. No calcula esos campos a partir del PID. Si no puede acreditar el vínculo con la identidad nativa de su propio lanzamiento, publica `commandRoot: null`; ante falta de vínculo, liderazgo o revalidación publica `scope: null`, fase `uncertain` y nunca `unix-scope-empty`. Para `remote-run`, ese vínculo exige que el padre lanzador conserve la referencia a su hijo raíz sin haberlo recogido, o que el supervisor conserve la identidad de nacimiento de esa raíz capturada durante su creación por un launcher confiable. La identidad es siempre la del runner raíz de ese lanzamiento, aunque no sea hijo directo del lanzador; nunca la del padre ni la de un intermediario. Leer luego un PID de disco, aunque `lstart` no sea posterior a `started` al segundo, no basta ante reutilización dentro del mismo segundo. El escritor revalida el vínculo tras `started` y antes y después de observar liderazgo. Una fecha de inicio posterior a `started` contradice la pertenencia; la igualdad al segundo por sí sola no la prueba. Esta obligación no depende de que SEN-3915r3 esté entregado. U1 no usa los marcadores `pid`/`started` de `remote-run` para establecer `commandRoot` ni inventa retrospectivamente el vínculo del escritor. Esta restricción no impide la unión del linaje ordinario por `runId` definida abajo.

En una corrida no `closed`, si la identidad observada de `commandRoot` coincide y la raíz vive, U1 compara el SID/PGID observados con `scope`. Si difieren, produce no confirmado por corrida con causa `liderazgo-no-concordante`; si no puede observar el liderazgo o `scope` no está disponible, produce no confirmado con causa `liderazgo-no-verificable`. La igualdad de números publicada ya fue validada como forma y no sustituye esta comprobación por observación. Las reglas de raíz viva no se aplican a `closed`, cuyo cierre se observa aparte.

En `unix-scope-empty` U1 exige además su propia observación completa de procesos: no hay procesos con ese sid **o** ese pgid. Si encuentra alguno, el resultado es no confirmado con causa `cierre-con-descendientes`. El linaje es el que la observación ordinaria de U1 ya asocia a la corrida remote-run cuyo id es exactamente `runId`. Un descendiente vivo fuera del SID/PGID enlazado cuenta como escape sólo si U1 verifica su identidad PID/inicio/boot en este ciclo; entonces el resultado es no confirmado con causa `cierre-con-escape`. Esa asociación por `runId` no atribuye dueño ni sustituye el vínculo nativo del escritor. Si no puede completar la observación de ámbito o del linaje disponible, incluida la revalidación de una identidad candidata, conserva no confirmado. Un escape sólo hipotético, que no se observa, no cambia el resultado informativo de cierre acreditado de ámbito; su posibilidad queda declarada como límite, y nunca prueba el árbol completo para soltar. `cmd.sh` o un descendiente que haga `setsid` propio o use control de trabajos puede salir de SID/PGID; ese cierre no acredita el fin de tales procesos. U1 no demuestra que un proceso jamás escapó ni crea un rastreador nuevo. El hostgate conserva íntegra la prueba de toda la carga y retiene cualquier duda de cobertura según `fleet/host-reservation@1`; scope vacío y este registro no bastan para soltar. El escritor no archiva ni marca `closed` una carga con escapes conocidos o cobertura de cierre insuficiente: conserva `uncertain`. U1 no adquiere autoridad de liberar ni de señalar por closure.

Los cuatro resultados también se prueban con registros cerrados directos y con una lectura que empezó antes del archivado. U1 no recorre `closed/` para buscar cierres antiguos.

## Retención del escritor

Fleet elige separar cerrados en **`closed/`**, manteniendo **N = 7 días**, equivalentes a 604 800 segundos desde `endedUtc`. Los cerrados archivados no cuentan para la enumeración ni los bytes por ciclo de U1.

Después de persistir cierre positivo, el escritor publica el registro cerrado completo y mueve atómicamente su directorio de `runs/<runId>` a `closed/<runId>` dentro del mismo filesystem. Sincroniza los directorios afectados. El destino no puede sobrescribir una corrida distinta. U1 puede observar la versión anterior, la cerrada durante esa transición breve, o ausencia después del rename; nunca debe inferir capacidad libre de esa ausencia. Si falla el archivado, Fleet conserva el registro y emite aviso; no declara concluido el archivado ni borra una reserva incierta.

El escritor mide la ocupación de `runs/`, con las mismas entradas que limita U1, y emite aviso al superar **2048 entradas**, el 50 % de 4096. Informa además el número archivado en `closed/` por separado. La medición no recorre `closed/` en nombre de U1 ni convierte falta de medición en ocupación cero. Separar el historial evita que siete días de cerrados, por ejemplo unas 585 corridas diarias, llenen por sí solos el censo activo. Sólo el escritor Fleet elimina sus registros `closed` con cierre positivo persistido y antigüedad superior a N. Antes de borrar, revalida identidad del archivo, reserva, generación y que no hay una reserva activa vinculada. No sigue symlinks ni borra fuera del directorio de esa corrida.

Los registros `starting`, `running`, `closing` o `uncertain` nunca caducan por esta regla. Una fecha futura, reloj no confiable o cierre no verificable conserva el registro y emite aviso. Borrar el registro no libera reservas ni borra workspaces, artefactos o recursos de carga. U1 no ejecuta retención. Los fallos de retención se notifican y no se convierten en prueba de cierre.

## Condición de adopción y evidencia exigida

Mini ya implementa U1 contra las decisiones PAO escritas aquí. Se leyó su diseño `~/.claude/encargos/mini-carga/sen3917/diseno-r1.md` para alinear resultados y lectura acotada. Este documento no añade un bloqueo de diseño; la aceptación de la entrega y la activación conservan sus puertas. El piloto actual aún no publica el registro. Hostgate sigue en sombra y los gates SEN-3900, SEN-3916 y SEN-3853 se mantienen.

Las regresiones requeridas del productor y consumidor cubren:

- Día de un dígito con doble espacio de `%e`, espacios finales, ambos caminos U1 y misma cadena UTC canónica.
- Zonas locales distintas y transición DST de SEN-3888, sin `time.mktime` ni comparación en hora local.
- Identidad Darwin primaria con boot y uniqueId. Dos procesos distintos; cambio de boot con no confirmado `otro-boot` antes de comprobar reutilización; boot no verificable con `boot-no-verificable`; observador indisponible; `birthSecondary: null` y EPERM sólo en el informe externo.
- PID reutilizado dentro del mismo segundo con uniqueId distinto, si se logra provocar con recursos propios. Si no se logra, se declara el límite runtime y se conserva una regresión con observaciones controladas que no se presenta como PID reutilizado observado.
- Los cuatro resultados; supervisor vivo con raíz terminada nunca dueño vivo; pérdida de cliente o supervisor y cierre positivo.
- `namespaceId` Darwin, physicalHostId y executorId informativos sin comparación. Rechazo de Linux como plataforma no soportada. UID del registro, archivo y lector concordantes, o registro inseguro.
- Frescura con umbral de tres intervalos y mínimo 300 segundos, campo ausente con 900 segundos; los avisos de edad, secuencia o `updatedUtc < startedUtc` no cambian atribución.
- Cada campo, tipo y concordancia de closure, comparación de instantes UTC parseados, precisión de hasta seis dígitos fraccionarios, never-created sin raíz ni scope; discordancia interna invalida el censo entero, mientras liderazgo observado no concordante sólo da no confirmado por corrida.
- Ausencia, truncamiento, claves duplicadas, symlink, modo o dueño inválidos, encoding o versión desconocidos y valores malformados.
- Límites de cantidad, bytes individuales y bytes agregados, con entrada inválida incluida, sin atribuciones parciales ni escrituras de U1.
- Archivo de cerrados fuera del censo U1, más de 4096 cerrados sin cegar el censo activo, transición de archivado y aviso al superar 2048 entradas activas. Retención a ambos lados de siete días, fechas futuras y estados no terminales conservados.
- Ubicación desde el home de la cuenta, invariable ante HOME u otro entorno del cliente; sustitución de raíz sólo por parámetro de pruebas.
- Gramática de runId, rechazo de `.next` y escape de ruta. Coexistencia con run.sh y mini.lock bajo una sola autoridad, sin atribuir desde turnos.
- Raíz runner correcta frente a `cmd.sh` y guardián; el vínculo con identidad nativa del lanzamiento se prueba ante PID reutilizado en el mismo segundo; rechazo del liderazgo observado no concordante y del no verificable; unión del linaje ordinario de remote-run por `runId` exacto; escape vivo con PID/inicio/boot verificados en el ciclo da `cierre-con-escape`, identidad candidata no revalidable conserva no confirmado sin inventar un escape; raíz sin vínculo queda null y un runner indirecto conserva su propia identidad; escape hipotético no altera el resultado informativo de ámbito, y cobertura insuficiente conserva la reserva. Son requisitos de regresión, no pruebas ejecutadas.

La evidencia host de Mini fue comunicada por PAO; Fleet no la reprodujo. La corrida viva `runs/20261024-135511-c78816` en Mini real observó runner PID70946, SID70946, PGID70946; hijo bash `cmd.sh` PID70956 en el mismo sid/pgid, sin liderarlo. Ambos remote-runs desplegados usan `nohup perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV'` sin verificar `setsid`. SEN-3915r3 exige garantizar y verificar liderazgo antes de publicar `pid`, haciendo fork previo si ya es líder. No se afirma que esa corrección esté entregada o probada ni que haya productor disponible con el enlace requerido. Si falta enlace, liderazgo o revalidación, `scope` queda null y la fase `uncertain`; no se sustituye por el PID de `cmd.sh` o del guardián. No se realizaron pruebas locales ni conexiones para redactarlo.
