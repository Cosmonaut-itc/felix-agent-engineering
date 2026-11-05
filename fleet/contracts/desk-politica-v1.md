# Política del desk, v1

Estado: implementación U1 para revisión. No acredita APTO ni autoriza instalación.
Propietario del archivo y del módulo: PAO.
Schemas: `pao/desk-policy@1`, `pao/desk-wait@1` y `pao/desk-admission@1`.
Implementación: [`fleet/deskpolicy/`](../deskpolicy/).
Ámbito: WSL Ubuntu del desk, 12 CPUs numeradas de 0 a 11, un uid cooperativo.
`fleet/hostgate/` permanece en sombra y no participa.

Este contrato incorpora el candidato Sol A y los injertos del veredicto.
Prevalecen las decisiones de Felix del brief U1 sobre presupuesto y afinidad.
El canario inicial usa 4 corridas y 8 unidades CPU.
Subir a 6 corridas y 10 unidades requiere una decisión registrada.
Una cuota cuenta corridas vivas. El peso cuenta CPUs asignadas de noche.
Ninguno de los dos presupuestos concede permisos fuera de una transacción.

## Rutas e instalación

Todas las rutas siguientes se resuelven en el desk. `~` es el home del uid
obtenido de la base de cuentas del sistema, nunca de `HOME` ni de XDG.

| Ruta | Autoridad y contenido |
| --- | --- |
| `/tmp/sen-test-slots/admission.lock` | Mutex común estable de PAO, Jale y Hasito. |
| `/tmp/sen-test-slots/pao-policy-v1.json` | Único archivo de política efímera. Sólo PAO lo publica. |
| `/tmp/sen-test-slots/pao-wait/<request_id>.json` | Una solicitud, su claim y su tombstone en el mismo archivo atómico. |
| `/tmp/sen-test-slots/slot-<n>.lock` | Flock de vida Jale, índice original 0 a 11. |
| `/tmp/sen-test-slots/slot-<n>/` | Metadatos Jale opcionales. Sólo existen para una claim v1 contabilizada. |
| `/tmp/sen-test-slots/pao-hasito/lane-<n>.lock` | Flock de vida remoto Hasito, índice 1 a 12. |
| `/tmp/sen-test-slots/pao-control.json` | Revisión monotónica del escritor. |
| `/tmp/sen-test-slots/pao-arm.json` | Preflight explícito, ligado a boot, configuración y hash del módulo. |
| `/tmp/sen-test-slots/writer.lock` | Instancia única del bucle de publicación. |
| `/tmp/sen-test-slots/pao-window.json` | Veto manual independiente de Jale. No lo sustituye v1. |
| `~/.config/pao/desk-policy.json` | Configuración administrativa persistente. |
| `~/.local/lib/pao-deskpolicy/v1/deskpolicy/` | Módulo instalado, fuera de los worktrees de consumidores. |
| `~/remote-run/runs/<run_id>/pao-admission.json` | Recibo Hasito persistido por el wrapper antes de ejecutar el comando. |

Directorios de estado y del módulo son reales, del uid actual y modo 0700.
Los archivos son regulares, del mismo uid, modo 0600 y con un solo enlace.
El recibo admite además la carpeta real de remote-run en 0755 o 0775.
El lector histórico de `pao-window.json` no restringe el modo del archivo.
Se rechazan symlinks, incluido cualquier componente del directorio.
Los adaptadores usan `umask 077` antes de abrir descriptores o crear metadatos.
Nunca truncan, borran ni reemplazan un inode de flock. Usan apertura sin truncar.

Cada JSON tiene hasta 65536 bytes UTF-8. Se rechazan NUL, claves duplicadas,
NaN, Infinity, campos desconocidos y booleanos donde se exige un entero.
Las excepciones son los campos históricos permitidos de `pao-window.json`.
Las escrituras de estado usan temporal hermano 0600, `fsync`, `os.replace` y `fsync`
del directorio, siempre bajo `admission.lock`. Un temporal `.pao-*` incompleto
no es una solicitud ni una claim y se ignora. No se ejecuta texto de un JSON.

## Archivo de política

Todos los campos de este ejemplo son obligatorios. No se permiten otros.

```json
{
  "schema": "pao/desk-policy@1",
  "revision": 81,
  "writer": "pao",
  "boot_id": "00112233-4455-6677-8899-aabbccddeeff",
  "issued_at": "2026-11-05T06:00:00Z",
  "expires_at": "2026-11-05T06:01:00Z",
  "issued_boottime_s": 1200,
  "expires_boottime_s": 1260,
  "mode": "night",
  "night_budget": 4,
  "cpu_weight_budget": 8,
  "max_run_weight": 4,
  "min_start_interval_s": 120,
  "load1_lt": 12,
  "limits": {"jale": 1, "hasito": 3},
  "pending": {"jale": 1, "hasito": 10},
  "algorithm": "pending-divisor-min1@1"
}
```

| Campo | Tipo y restricción |
| --- | --- |
| `schema`, `writer`, `algorithm` | Literales del ejemplo. |
| `revision` | Entero positivo, como máximo 2^63−1. No se reutiliza durante una instalación sin pérdida del estado. |
| `boot_id` | UUID minúsculo de `/proc/sys/kernel/random/boot_id`, igual al boot actual. |
| `issued_at`, `expires_at` | Calendario UTC estricto `YYYY-MM-DDTHH:MM:SSZ`. Sin fracciones ni offsets. |
| `issued_boottime_s`, `expires_boottime_s` | Enteros no negativos de `CLOCK_BOOTTIME`. Incluyen suspensión. |
| `mode` | `day` o `night`. |
| `night_budget` | Entero 1 a 12. El límite de validación no aprueba ampliar el canario. |
| `cpu_weight_budget` | Entero 1 a 12, presupuesto nocturno global de pesos. |
| `max_run_weight` | Entero 1 a `min(4, cpu_weight_budget)`. Peso máximo admitido por corrida. |
| `min_start_interval_s` | Entero exactamente 120 en producción v1. |
| `load1_lt` | Entero 12 de noche o 9 de día. No es un valor configurable por consumidor. |
| `limits`, `pending` | Objetos con exactamente `jale` y `hasito`, enteros no negativos hasta 2^63−1. |

La duración UTC es positiva, como máximo 60 segundos, y coincide exactamente
con la duración BOOTTIME. Ambos relojes cumplen `issued <= ahora < expires`.
La diferencia entre el tiempo transcurrido de ambos relojes es menor de 2 s.
La tolerancia cubre el truncamiento de los dos relojes a segundos enteros.
Una incoherencia, una fecha futura o un boot distinto invalida el archivo.

Noche es `[05:00Z,14:00Z)`, todos los días. Se comprueba la hora del desk bajo
el mutex, también en el último control antes de guardar la claim.
Una política nocturna debe emitirse dentro de esa ventana y vencer como máximo
a las 14:00Z del mismo día. A las 14:00Z ya rige día.
La suma de cuotas nocturnas no supera `night_budget`.
De día las cuotas son exactamente 2 para cada proyecto.

Archivo ausente, corrupto, inseguro, vencido o de versión desconocida selecciona
día. Nunca se reutiliza la última noche válida. Esto no certifica un censo sano.
Si el censo, el módulo o sus locks fallan, no se admite y se devuelve 75.

## Peso, día y afinidad

En día se admiten como máximo 4 corridas conjuntas y 2 por proyecto.
Jale permite una `chain` y una `verify`. `exclusive`, incluido web-unit completo,
excluye otras Jale. La carga Jale debe ser finita y estrictamente menor de 9.
Hasito sólo crea nuevas reservas en carriles 1 o 2 y no recibe un umbral de
carga diurno nuevo. Los recursos locales de cada consumidor siguen vigentes.

Las claims nocturnas, incluidas las inciertas, conservan CPUs, tipo y peso
después del amanecer o de vencer la política. Siempre cuentan al admitir.
De día el peso no acopla a los proyectos ni limita su afinidad. Cada admisión
diurna recibe las 12 CPUs, aunque se solapen con otras claims. No hay presupuesto
CPU diurno. El máximo declarado es el del archivo válido; en fallback es 4.
Al volver a noche, las claims diurnas todavía vivas conservan sus 12 CPUs
y frenan nuevas asignaciones nocturnas hasta drenar. No se les cambia afinidad.
El espaciado sólo frena noche, pero toda admisión diurna queda en el registro.

Los consumidores declaran el peso mediante esta tabla normativa de PAO.

| Consumidor y tipo de corrida | Peso |
| --- | --- |
| Jale `gates`, `api-gates`, `mobile-gates`, `types`, `lint`, `packages`, `packages-build`, `web-unit`, `e2e` | 4 |
| Jale `contract` y verify mediante `host-session.sh launch` | 2 |
| Jale `api-unit` y `host-tests` | 1 |
| Hasito `script` | Entero de 1 a 4 declarado por el guion; por omisión, 4. |

U2 fija Turbo `--concurrency=4` en `packages` y `packages-build`.
Los tipos `chain`, `verify` y `exclusive` conservan su función de exclusividad.
El adaptador Jale traduce el comando a peso antes de registrar la solicitud.
La CLI exige `--weight`; el adaptador Hasito materializa la omisión como 4.
No hay catálogo de perfiles ni certificación de guiones en v1.

Al registrar se exige `weight <= max_run_weight` de la política efectiva.
Un peso mayor devuelve 75 y `reason=weight-too-large`, sin crear solicitud.
Una pendiente que supera un máximo reducido deja de participar en FIFO.
El writer o su renovación la marca `expired`, con `last_reason=weight-too-large`.
Una claim ya admitida conserva su peso y capacidad hasta cierre acreditado.

De noche se asignan las primeras `weight` CPUs libres, en orden numérico.
No se reutiliza una CPU de una claim activa o incierta para una entrada nocturna.
La topología de producción exige CPUs online 0 a 11.

Antes de ejecutar cualquier limpieza, preparación costosa o guion, el wrapper
ejecuta `/usr/bin/taskset -c <lista-del-recibo> <comando>`. Todo su árbol hereda
esa afinidad. Jale conserva el mismo requisito durante el handoff al keeper.
El supervisor y su carga pertenecen a una sesión Linux identificada.
El wrapper no ejecuta carga antes de la admisión. Su propia lógica de control no es una corrida adicional.

`taskset` limita CPUs ejecutables simultáneamente para el árbol cooperativo.
No limita memoria ni los contenedores de Docker Desktop. Tampoco contiene
código del mismo uid que cambie su afinidad o escape de la sesión.
Los consumidores prohíben escapes deliberados y no certifican esas cargas
como contenidas por v1. PAO incluye recursos externos y puertos en el preflight
y en cualquier recuperación. No se afirma aislamiento frente a código hostil.

## Solicitudes y claims

`request_id` y `run_id` cumplen `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`.
Una invocación registra una solicitud antes de tomar carril o sincronizar.
Renueva cada 30 s, también mientras espera mirror o despacho.
El TTL de espera es 120 s BOOTTIME. La secuencia se asigna bajo el mutex como
el máximo del boot más uno. No cambia al renovar.

Cada archivo `pao-wait/<request_id>.json` contiene exactamente estos campos.

```json
{
  "schema": "pao/desk-wait@1",
  "request_id": "invocacion-1",
  "project": "hasito",
  "kind": "script",
  "owner": "sesion-PAO",
  "boot_id": "00112233-4455-6677-8899-aabbccddeeff",
  "seq": 1,
  "state": "pending",
  "admitted_run_id": null,
  "renewed_boottime_s": 1200,
  "expires_boottime_s": 1320,
  "weight": 2,
  "claim": null,
  "last_reason": null
}
```

`project` es `jale` o `hasito`. Jale admite `chain`, `verify`, `exclusive`.
Hasito admite `script`. `owner` es texto descriptivo de 1 a 256 caracteres.
`seq` es entero positivo. Los tiempos son enteros no negativos y vencimiento
es renovación más 120. `last_reason` es null o un motivo de la tabla inferior.

Registrar o renovar la misma identidad y contenido es idempotente.
Cambiar boot, proyecto, tipo, owner o peso con el mismo id es conflicto.
Los estados de solicitud son `pending`, `admitted`, `withdrawn` y `expired`.
Sólo `pending` puede renovarse o retirarse. El vencimiento no resucita.
Una retirada de una admitida devuelve su estado y conserva la claim.
Los tombstones permanecen todo el boot, incluidos los de claims cerradas.
Los clientes usan un id nuevo después de una solicitud terminal sin claim.

La transición a `admitted` guarda la solicitud y la claim en un único replace.
`admitted_run_id` coincide con `claim.run_id`. Esa claim contiene exactamente:

| Campo de claim | Valor |
| --- | --- |
| `run_id`, `lane`, `weight`, `cpus`, `boot_id` | Identidad remota, carril lógico 1 a 12, peso declarado, lista de CPUs distintas 0 a 11 y boot de admisión. Su ancho es el peso de noche o 12 de día. |
| `process` | Objeto exacto `pid`, `start`, `pgid`, `sid`, `zombie`. Los primeros cuatro son enteros positivos. `start` es el campo 22 de `/proc/<pid>/stat`. `zombie` es false al admitir. |
| `state` | `active`, `uncertain` o `closed`. |
| `admitted_boottime_s` | BOOTTIME del commit de admisión, dentro del arriendo de espera. |
| `closed_boottime_s`, `close_evidence` | Null hasta cierre. En cierre acreditado son BOOTTIME y `session-empty`. |
| `claim_token` | 32 bytes aleatorios codificados como 64 hexadecimales minúsculos. El admisor lo entrega al supervisor para su handoff. |
| `script_exit_code` | Null antes de terminar el guion, después entero 0 a 255. Se conserva aunque queden hijos y la claim esté incierta. |
| `close_cause` | Null hasta cierre, después `completed` o `stopped`. |
| `mode`, `revision` | Política realmente usada. `revision` es null en fallback. |

El carril lógico Jale 1 se corresponde con el slot histórico 0. El 12 se
corresponde con el slot 11. No se renombran sus directorios ni sus tipos.
Hasito usa el mismo índice lógico 1 a 12 para sus locks remotos y locales.

La última admisión del boot es el máximo `admitted_boottime_s` entre todas las
claims del boot, también las cerradas. No hay un segundo archivo que pueda
perder el timestamp después de guardar una claim. Los reintentos del mismo
request y run devuelven `replay=true`, `execute=false`, incluso tras su cierre.
Nunca autorizan otra ejecución. Un run o lane diferente es conflicto.

## Censo y cierre conservador

El censo lee todas las solicitudes y claims, sin recortar por la cuota actual.
Suma activas e inciertas de ambos proyectos y comprueba CPUs nocturnas disjuntas.
Una claim no cerrada con flock libre pasa a `uncertain` y conserva corrida,
peso, recurso y CPUs. La transición se persiste en admisión o publicación.
`estado` la muestra sin escribir. Una claim incierta no se vuelve activa
porque alguien vuelva a tomar el flock.

Un flock ocupado sin claim conocida, un estado ilegible, un recurso duplicado,
un BOOTTIME futuro, una claim de otro boot o CPUs nocturnas solapadas hacen desconocido
el censo. Devuelven 75, sin vista parcial. Un slot legacy sin claim v1 también
bloquea. Los flocks estables libres sin directorio de carga no consumen cupo.
Los metadatos Jale sólo se mantienen para slots con claim conocida.
Jale retira su directorio de metadatos tras `cerrar`, en la misma toma del
mutex, antes de liberar fd 9. Conserva siempre el archivo del flock estable.
No se adopta carga legacy por PID ni por mtime durante la convivencia.

`cerrar` se ejecuta bajo el mutex, conservando el flock de vida heredado.
Comprueba run, boot, token de claim y el mismo inode de flock retenido.
El token viaja con fd 8 desde el supervisor al keeper. La igualdad de sesión
no acredita ese handoff. Un token incorrecto o ausente devuelve 75 sin cerrar.
El keeper puede abrir su propia sesión mediante `setsid`, como hace Jale.
El cierre exige que tanto la sesión original como la sesión del keeper estén
vacías de procesos no zombies, salvo el supervisor original con PID e inicio
comprobados, el keeper que cierra y el proceso Python de la operación.
El PID declarado debe ser el del propio proceso o su padre directo.
El token se guarda sólo en estado y recibos privados. No se pasa por entorno,
argumentos del proceso ni logs de texto; `cerrar` lo lee de un fd heredado.
El cliente escribe los 64 caracteres ASCII del token en una sola escritura
al canal y cierra su extremo escritor. Cada intento de cierre recibe un canal
nuevo con el mismo token. Un intento fallido no requiere otro token de claim.
Un segundo handoff que cambie de sesión requiere reconciliación, no se presume
cubierto por la sesión original y la del primer keeper.
Un cierre fallido conserva capacidad. Tras el commit de cierre, el supervisor
cierra su descriptor. Un handoff sólo cierra la copia del descriptor, nunca
ejecuta `flock -u`, porque comparte la descripción abierta con el keeper.

La ausencia de PID, de heartbeat, de ACK o el TTL no acreditan cierre.
SIGKILL del wrapper con hijo vivo conserva la claim como incierta.
Los guiones no deben usar setsid, doble fork ni trabajo deliberadamente
desacoplado. El kernel no permite probar desde esta sesión la ausencia de
trabajo que haya escapado. Tal caso requiere reconciliación operativa.
La recuperación de una incierta no mata procesos ni borra una reserva para
abrir cupo. En v1 no hay recolector ni comando de liberación forzada.
PAO debe acreditar fin del run, árbol y recursos externos antes de corregir
atómicamente o archivar su registro bajo el mutex, conservando evidencia.
Una claim no reconciliada impide rearmar si pertenece a un boot anterior.

## Reparto nocturno

El writer corre en WSL cada 20 s y recalcula al arrancar.
Cada tick del bucle tiene un timeout de 10 s. Un tick fallido no renueva noche.
`W` cuenta sólo solicitudes pendientes vigentes. No incluye admitidas ni
vuelve a sumar las filas de la MacBook. `L` cuenta activas e inciertas.
Cada proyecto conserva FIFO por `seq`. Una exclusive Jale en cabeza frena
adelantamientos Jale. La preparación Hasito también se serializa en cabeza.

```text
q[jale] = q[hasito] = 0
elegibles = proyectos con W > 0
orden = menor seq de cabeza, después nombre de proyecto
para proyecto en elegibles, según orden:
    si suma(q) < N: q[proyecto] += 1
mientras suma(q) < N:
    candidatos = elegibles con q < L + W
    si no hay candidatos: terminar
    elegir mayor W / (q + 1), empate por orden
    q[elegido] += 1
```

La comparación usa fracciones enteras exactas.
Con N=4, espera 1 y 10 sin vivos produce cuotas 1 y 3.
Con N=6, los ejemplos 1/10, 10/1, 0/10 y 0/0 producen 1/5, 5/1, 0/6 y 0/0.
Con una pendiente por lado y ningún vivo produce 1/1.
N=1 prioriza la cabeza más antigua.
Las cuotas pueden caer por debajo de los vivos. Sólo frenan nuevas entradas.
Una demanda nueva tarda hasta un tick en cambiar el reparto publicado.
No se promete una proporción instantánea ni utilización perfecta.

## Transacción de admisión

`admitir` se llama en el desk mientras el consumidor mantiene el mutex común.
No admite con una vista leída antes de tomarlo. La API verifica que el fd del
mutex pertenece al inode correcto y a la descripción abierta que posee el flock.
El supervisor es líder de su propia sesión y grupo, con `pid=pgid=sid`.
La operación corre en esa misma sesión, en el supervisor o en su hijo directo.
La identidad de `/proc` debe pertenecer al uid actual.

La transacción sigue este orden:

1. Lee política y ambos relojes actuales. Una política inválida selecciona día.
2. Valida todo el registro y censa todos los recursos. Persiste incertidumbre.
3. Resuelve reintentos por request y run sin volver a ejecutar.
4. Exige solicitud pendiente vigente, del boot actual y cabeza de su proyecto. Rechaza con `resource-busy` si cualquier claim no cerrada, activa o incierta, ya reserva ese proyecto y carril. Lo comprueba antes de tomar flock, incluso si el supervisor muere después del censo.
5. Comprueba peso máximo, índice permitido, presupuesto total y cuota del proyecto.
6. Aplica límites locales que sólo reducen capacidad y exclusividad Jale.
7. Aplica el veto `pao-window.json`. Jale mantiene además su lector histórico.
8. Comprueba carga finita bajo el umbral efectivo, cuando corresponde.
9. De noche comprueba suma de pesos y asigna CPUs libres disjuntas. De día entrega las 12 CPUs sin acoplar pesos.
10. En noche exige al menos 120 s desde cualquier última admisión del boot.
11. Verifica identidad del supervisor y toma el flock de vida ya abierto por él.
12. Vuelve a validar relojes, política y solicitud justo antes del commit.
13. Escribe solicitud admitida, claim y timestamp juntos antes de liberar el mutex.

Si vence durante el despacho, devuelve 75 sin claim nueva. El siguiente intento
selecciona día. Si falla una escritura después de replace, el cliente no ejecuta
sin recibo. Consulta el mismo request y run para resolver el resultado ambiguo.
El registro durable puede conservar capacidad aunque el cliente no reciba ACK.

El mutex tiene espera máxima de 5 s. No se mantiene durante SSH, git, rsync,
sleep, ejecución del guion, preparación o limpieza de servicios.
Si se deniega después de tomar el fd de vida, el consumidor cierra ese fd antes
de esperar. Si se admite, lo conserva hasta cierre acreditado o handoff.
Nunca libera el mutex ni sus locks mediante replace o borrado del archivo.

Una reducción de N con más vivos que el presupuesto produce cero admisiones
hasta drenar por debajo del nuevo N. Aumentar la cuota del otro proyecto no
elude el presupuesto total ni el peso de los vivos.

`pao-window.json` conserva `until`, `reason` y `jale_slots` históricos.
Una ventana activa permite como máximo una Jale no exclusive si `jale_slots=1`.
Cualquier otro allowance efectivo es cero. Ventana ilegible veta Jale.
Una ventana vencida y legible se ignora. Sus timestamps admiten fracciones UTC.
El lector abre sólo para lectura y acepta cualquier modo de archivo regular
del uid actual, sin symlinks y con un enlace. Así una ventana vencida en 0644
vuelve a ser inerte. Un error de lectura sigue vetando Jale.
El escritor de ventanas debe usar un temporal hermano `.pao-window-*` en 0600,
seguido de fsync, replace y fsync del directorio bajo el mutex. El censo ignora
ese patrón por ser parte de `.pao-*`, incluso con JSON incompleto.
El lector sigue rechazando claves duplicadas y JSON malformado.

## CLI y recibos

La llamada instalada es siempre:

```sh
/usr/bin/python3 -I "$HOME/.local/lib/pao-deskpolicy/v1/deskpolicy/cli.py" OPERACION
```

El lanzador debe fijar la ruta desde la cuenta del sistema. El ejemplo usa
`HOME` sólo como abreviatura humana. No se usa `PYTHONPATH` del consumidor.
Sin harness, la CLI no acepta overrides de reloj, carga, boot, rutas ni
presupuestos y exige la instalación fija. Los valores ambientales de política
se ignoran. Las pruebas pueden usar `testing.Harness` dentro de unittest.

La CLI real bajo `RUN_SUITE_HARNESS` no vacío acepta estas costuras privadas.
Todas las rutas son absolutas, sin symlinks y dentro del cwd del mirror.

| Costura | Uso bajo el harness |
| --- | --- |
| `LOCK_ROOT` | Raíz de locks obligatoria. Nunca `/tmp/sen-test-slots`. |
| `PAO_DESK_HARNESS` | Directorio privado 0700 que contiene `LOCK_ROOT`; por omisión es su padre. Configuración y recibos de prueba viven debajo. |
| `PAO_DESK_CLOCK_FILE` | JSON 0600 exacto `{"utc":"2026-11-05T06:00:00Z","bt":1000,"boot":"00112233-4455-6677-8899-aabbccddeeff"}`. Se relee en cada muestra para probar reloj y boot. |
| `LOADAVG_FILE` | Primer número de la primera línea como carga; ilegible o inválido no autoriza Jale ni noche. |
| `PORT_LISTENERS` | Ejecutable privado de prueba utilizado por los probes Jale. El módulo valida la ruta y lo deja al adaptador; no ejecuta probes ni sustituye la evidencia externa del preflight. |

Sin archivo de reloj se usan valores deterministas del harness. Las CPUs
contables son 0 a 11; los guiones de prueba mantienen taskset 0 a 1.
Sin `RUN_SUITE_HARNESS`, estas variables no cambian ninguna entrada del módulo.
U2 propaga la llave de SEN-4644 y las rutas al proceso Python aislado con `-I`.
Ninguna costura permite configurar presupuestos desde el entorno.

| Operación | Argumentos y resultado |
| --- | --- |
| `publicar` | Bucle de 20 s con `writer.lock`. `--una-vez` publica un tick. `--desactivar-noche` persiste `night_enabled=false` y publica día. |
| `pendiente` | `--request-id`, `--project`, `--kind`, `--owner`, `--weight`. `--retirar` retira sólo una pendiente. Devuelve el registro. |
| `admitir` | `--request-id`, `--run-id`, `--lane`, `--pid`; fds heredados `--mutex-fd 9`, `--life-fd 8`. `--local-limit` sólo reduce cuota. Devuelve recibo. |
| `cerrar` | `--request-id`, `--run-id`, `--pid`, mismos fds heredados y `--claim-token-fd`. `--script-exit-code` y `--cause completed|stopped` registran fin. Devuelve el registro, o 75 conservando claim. |
| `estado` | Vista coherente de sólo lectura bajo el mutex existente. `--request-id` añade el registro de esa identidad. No crea estado ni persiste incertidumbre. |
| `preflight` | Comprueba instalación, topología, herramientas, censo y configuración. `--preparar` crea sólo directorios privados ausentes. `--armar --evidencia <archivo>` exige censo vacío y evidencia actual. |
| `ejecutar` | Wrapper opcional, `--request-id`, `--run-id`, `--lane`, `--local-limit`, `-- <argv>`. Usa la sesión del runner, admite, aplica taskset y acredita cierre. No espera ni renueva una solicitud denegada. |

`ejecutar --recibo` escribe el recibo Hasito en la ruta fija de runs, cuyo
directorio real debe ser 0700, 0755 o 0775 y del uid actual. El recibo es 0600.
La carpeta y la identidad de cualquier recibo previo se validan bajo el mutex
antes de guardar una claim. Un error de validación deja cero claims nuevas. No permite redirigirlo por entorno.
También imprime el recibo JSON en la primera línea de stdout, con `claim_token=null`
para evitar registrar el token privado en el log del guion.
La salida posterior pertenece al comando y a su log.
Un reintento con recibo ya admitido consulta el registro; jamás arranca otro guion.
El cliente que integra este wrapper debe conservar el recibo admitido original.

El recibo contiene exactamente los siguientes campos, salvo `policy_reason`
en un rechazo normal y `detail` en un error de infraestructura:

```json
{
  "schema": "pao/desk-admission@1",
  "request_id": "invocacion-1",
  "run_id": "20261105-060000-abcdef",
  "state": "admitted",
  "revision": 81,
  "mode": "night",
  "lane": 1,
  "reason": null,
  "exit_code": null,
  "cpus": [0, 1],
  "weight": 2,
  "replay": false,
  "execute": true,
  "claim_state": "active",
  "claim_token": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "script_exit_code": null,
  "close_cause": null
}
```

Una denegación lleva `state=denied`, `exit_code=75`, `execute=false`, CPUs
vacías y peso null. Una admisión lleva `exit_code=null`, incluso si el guion
termina con 75. El recibo final separa `script_exit_code` de `claim_state`.
Si queda un hijo vivo, el wrapper devuelve el código real del guion y conserva
`claim_state=uncertain`, peso, CPUs y recurso. Un guion que sale 3 no se convierte
en una denegación 75. `claim_state` de una repetición puede ser closed o uncertain.
`admitir` retorna 0 en admisión o repetición y 75 en denegación.
Sólo `execute=true` autoriza ejecutar una vez. Un JSON ausente o inválido no
autoriza. Los comandos administrativos fallidos devuelven `pao/desk-error@1`
con `reason=census-unknown`, `detail` y `exit_code=75`.

Los motivos de recibo y rechazo son un conjunto cerrado:

| Motivo | Significado |
| --- | --- |
| `policy-invalid`, `policy-expired`, `window-closed` | Razón de fallback o vencimiento durante la transacción. |
| `load-high` | Carga ilegible, no finita o por encima del umbral efectivo. |
| `quota-full`, `budget-full`, `weight-full`, `cpu-full` | Cuota, corridas globales, peso o CPUs disponibles insuficientes. |
| `start-interval`, `fifo` | Freno global nocturno o solicitud que no es cabeza. |
| `resource-busy`, `local-limit`, `legacy-freeze` | Carril, exclusividad, límite local o veto manual. |
| `census-unknown`, `mutex-error`, `internal-error` | Infraestructura sin garantía para admitir. |
| `request-expired`, `request-terminal`, `request-conflict`, `weight-invalid`, `weight-too-large` | Identidad, estado o peso no admisibles. |
| `already-admitted`, `close-unproven` | Identidad ya admitida o cierre sin prueba. La repetición normal usa `replay=true` con motivo null. |

`estado` muestra modo, revisión, vencimiento, presupuesto efectivo, cuotas,
pendientes, cabezas, vivos por proyecto, activas, inciertas, peso y CPUs por claim,
carga, última admisión y último rechazo de cada solicitud.
Los rechazos son persistidos en la propia solicitud. La vista puede contener
motivos históricos. Un error de censo no devuelve esta vista parcial.

## Configuración, arranque y reinicio

La configuración tiene exactamente los campos de este ejemplo:

```json
{"schema":"pao/desk-config@1","night_enabled":false,"night_budget":4,"cpu_weight_budget":8,"max_run_weight":4,"min_start_interval_s":120}
```

Los presupuestos tienen las mismas restricciones del archivo efímero.
`night_enabled` es booleano. La instalación crea false.
La configuración no cambia mediante variables de shell ni flags de consumidor.
Una edición administrativa usa publicación atómica bajo el mutex.

El preflight armado exige censo vacío y un archivo privado con este esquema:

```json
{
  "schema": "pao/desk-preflight@1",
  "boot_id": "00112233-4455-6677-8899-aabbccddeeff",
  "checked_at": "2026-11-05T06:00:00Z",
  "consumers": {"jale": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "hasito": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
  "resources_checked": true,
  "legacy_drained": true
}
```

`checked_at` no puede estar en el futuro ni tener más de 300 s.
Los consumidores son SHAs de 40 caracteres hexadecimales minúsculos.
Las dos comprobaciones verdaderas son evidencia administrativa aportada por PAO,
no mediciones automáticas del módulo. PAO comprueba entradas migradas, puertos,
servicios y recursos Docker Desktop antes de afirmarlas.
El resultado incluye versión y hash determinista de los siete archivos Python
del runtime. El armado guarda también configuración aprobada y hashes de
configuración y evidencia. Presupuestos y peso máximo pueden reducirse sin
rearmar. El writer publica esos límites reducidos con las claims vivas intactas.
Una ampliación por encima de lo armado, activar noche desde un armado diurno,
un cambio de código o un boot distinto exige preflight nuevo con drenado.

Sin armado vigente el writer publica día aunque la configuración pida noche.
La publicación nocturna exige además ventana UTC y censo sano.
Si falla un tick, no renueva el arriendo previo. Su vencimiento devuelve día.
El control de revisión se guarda antes de publicar, por lo que un fallo puede
saltar una revisión pero no reutilizarla en el siguiente tick.

PAO inicia explícitamente un único writer desacoplado de stdin y SSH, en sesión
propia, con stdout y stderr en un log privado. No se requiere sudo ni linger.
No se promete reinicio automático ni supervivencia del user manager.
PAO debe comprobar la supervivencia real al cerrar SSH antes de habilitar noche.
Si faltan la raíz o sus subdirectorios después del reinicio, el módulo los
crea idempotentemente en 0700 y abre `admission.lock` en 0600 sin truncarlo.
Sin archivo de política rige día, sin intervención de PAO. Una raíz existente
con modo incorrecto falla y exige el corte; el módulo no cambia sus permisos.
La noche sigue exigiendo preflight y arranque explícito del writer de PAO.
Un boot nuevo no adopta metadatos viejos. Si sobreviven claims antiguas en el
filesystem, bloquean hasta reconciliación. Reiniciar no acredita limpieza Docker.

## Integración Jale

`run-suite.sh` registra su RUN_ID antes de esperar y renueva cada 30 s.
Su límite `MAX_SLOTS` diurno y `DESK_LOAD_LIMIT=9` siguen readonly.
Un `MAX_SLOTS` explícito válido 1 o 2 sólo reduce también noche.
Un valor mayor nunca amplía cuota. SEN-4632 y el harness SEN-4644 se conservan.
La información nocturna se lee de nuevo dentro de cada toma del mutex.

El slot original `n` pasa como `--lane n+1`. Se conserva fd 8 sobre el inode
`slot-n.lock`. El consumidor abre fd 8 sin truncar y aún sin tomar flock,
hereda fd 9 ya bloqueado y llama `admitir`. La operación toma flock de fd 8.
Los metadatos `kind`, `run_id`, `owner` y `since` mantienen sus nombres.
Su preparación y los probes de puertos ocurren con claim, fuera del mutex.
Un fallo de preparación requiere cierre acreditado, no borrar la claim.
Los índices nuevos pasan los mismos guards de BD, puertos y worktrees.

`host-session.sh` conserva el descriptor de vida sin intervalo libre al keeper.
Los hijos de carga cierran fds 8, 9 y el fd del token. Heredan afinidad y la sesión del keeper.
La pérdida del keeper sin cierre conserva incertidumbre.
El keeper conserva su `setsid` actual, el aislamiento de SIGHUP y stop por grupo.
El launcher le transfiere fd 8 y el token de la admisión por un canal privado
antes de cerrar su copia. El keeper conserva el token en un fd privado y lo
pasa mediante `cerrar --claim-token-fd <fd>` antes de cerrar fd 8. No necesita
pertenecer a la sesión original para acreditar identidad. Ambas sesiones se
comprueban vacías según el cierre normativo.
`verify.sh` prueba conectividad y deja la decisión desktop al desk.
El ratio de carga en la laptop sólo puede ordenar preferencias `auto`.
La ruta Mini no cambia.

`SLOT_POLICY=none` y `DESK_SLOT_OVERRIDE` no saltan ninguna condición v1.
El override puede quedar como motivo de auditoría, sin ampliar capacidad.
Jale mantiene la precedencia del veto manual y sus restricciones de recursos.
Un módulo inaccesible, error o recibo inválido da 75, sin ejecutar por intuición.

## Integración Hasito y remote-run

`run.py --entorno desk` valida el host efectivo de remote-run.
Registra una solicitud remota antes de buscar carril o mirror y la renueva
durante toda espera, incluso mientras un start tarda en responder.
Sólo la cabeza Hasito prepara un despacho.
Primero toma una reserva local duradera por SHA-256 de `realpath(worktree)`.
Después toma el primer carril canónico libre. El mirror se protege antes de
cualquier `rsync --delete`. La reserva vive fuera del mirror y dura hasta
recuperar la evidencia. Dos carriles no sincronizan el mismo mirror a la vez.

Los carriles de la MacBook son `~/.local/state/comprador/desk/desk.lock`,
`desk-2.lock` hasta `desk-12.lock`. Un `--lock` explícito sólo acepta uno de
esos nombres y rutas reales. De día sólo se crean nuevas reservas 1 o 2.
De noche los índices candidatos llegan hasta N. Las reservas altas ya vivas
siguen contando si baja N. Los locks locales conservan sync y recuperación.
El flock remoto conserva ejecución. No son intercambiables.

`remote-run` recibe `--desk-request <id>` y `--desk-lane <n>`.
El runner usa el entrypoint PAO instalado antes de limpiar reportes o ejecutar
`cmd.sh`. Puede usar `ejecutar --recibo` para poseer claim y afinidad.
El runner ya debe ser líder de sesión y grupo; el wrapper es su hijo directo
o es él mismo ese líder. El wrapper no llama `setsid`. Si es hijo del runner,
pone sólo su proceso de control en otro grupo de la misma sesión. El guion
y su árbol se unen al grupo original del runner antes del exec de taskset.
Así TERM y KILL de `remote-run stop` llegan al guion y el control sobrevive
para esperar el resultado, persistir 143 o 137 y cerrar con causa `stopped`
cuando no quedan hijos. Un TERM dirigido al wrapper se reenvía al guion.
Un SIGKILL dirigido al propio wrapper conserva la regla de incertidumbre;
si queda carga viva, nunca se regala capacidad.
La carpeta del run admite los modos históricos 0755 y 0775 además de 0700.
Un runner que espera puede seguir figurando `corriendo` en la interfaz antigua.
El consumidor escribe `ADMISSION_WAIT` y `ADMITTED request_id=... revision=... lane=...`
en su log. El censo usa la claim, nunca cuenta el PID esperando como otra corrida.

El transporte añade `admission <run_id>` para consultar el recibo fijo.
`start_code=0` sólo confirma despacho, no admisión.
Un denied remoto conserva logs y recibo, devuelve 75 y omite reportes inexistentes.
Un guion admitted que devuelve 75 sigue el camino normal de evidencia fallida.
Un 255, ACK perdido, ID ausente o recibo inconsistente conserva recuperación.
Los ids de request, run, lane y mirror quedan enlazados en las reservas locales.
La rama Mini y su FIFO por candado no cambian.
Las sesiones viejas se drenan. No se inventa un FIFO histórico entre carriles.

## Corte, retirada y aceptación

Los guiones `deploy-desk.sh` y `withdraw-desk.sh` se entregan sin ejecutar.
Ambos exigen una referencia `--apto`. El primero valida todas las precondiciones
antes de instalar código o configuración. Sólo instala día y no inicia el writer.
Una instalación idéntica con la misma referencia se puede repetir. Un runtime,
manifiesto o referencia distintos se rechazan sin modificar los existentes.
La configuración existente debe ser válida y tener noche desactivada.

El orden de corte sobre el estado histórico del desk es obligatorio.

1. Pausar nuevos emisores y el writer. Drenar corridas y pendientes con sus dueños, conservando evidencia y reservas locales hasta recuperar resultados.
2. Conservar `admission.lock` existente y tomarlo sin truncar. Comprobar bajo ese mutex que todos los flocks de vida están libres y no quedan procesos ni recursos externos atribuidos. Un modo histórico 0644 no acredita libertad.
3. Cambiar la raíz existente a 0700. Aplicar `chmod 600` al mismo inode de `admission.lock`, `writer.lock` si existe y cada `slot-*.lock` y `pao-hasito/lane-*.lock` existente. No truncar, reemplazar ni borrar locks. Registrar sus inodes antes y después.
4. Archivar mediante rename los directorios históricos `slot-n/` ya drenados fuera de la raíz, en un directorio privado de evidencia de PAO. No borrar metadatos ni mover los archivos `.lock`. Si no se acredita drenado, detener el corte.
5. Soltar el mutex y ejecutar el despliegue posterior al APTO. El instalador comprueba raíz 0700, todos los locks 0600, ausencia de flocks vivos, censo vacío, pendientes drenadas, política de día, destinos privados y contenido de cualquier instalación previa. Una precondición fallida no publica runtime ni configuración.
6. La publicación usa staging privado y renames bajo el mismo mutex. Un error detectado revierte los nombres nuevos al staging y conserva evidencia. La reejecución recupera una publicación idéntica interrumpida; nunca sobrescribe contenido distinto. Los temporales propios conservados no son capacidad ni autorizan noche.
7. Validar día con ambos consumidores. Preparar evidencia de recursos, armar y arrancar explícitamente el writer antes del canario nocturno.

El instalador no hace los chmod ni archiva por su cuenta. Ante locks históricos
0644 falla antes de instalar. El corte y su vuelta conservan todos los archivos.
El segundo persiste noche desactivada y publica día sin borrar reservas,
matar corridas ni retirar los binarios que los consumidores aún necesitan.

El corte exige consumidores revisados, inventario de emisores actualizado,
ausencia de entradas directas y ad hoc, censo vacío y migración de pendientes
por sus dueños con nuevos ids y orden verificable.
Las primeras observaciones se hacen de día. El canario nocturno se habilita
tras APTO, gates y verificador. Hay que acreditar expiración, amanecer y caída
del writer antes de dejarlo sin vigilancia.
El rollback baja a día y drena antes de revertir consumidores o índices altos.

Las pruebas PAO están en `fleet/deskpolicy/tests/`. Se ejecutan en un mirror
temporal propio de remote-run, una corrida a la vez, bajo `taskset -c 0-1`.
El arranque exige carga menor de 12 y al menos 120 s desde el último arranque
en `~/remote-run/runs`. El script vuelve a comprobar ambos gates en destino.
No toca `/tmp/sen-test-slots` ni estado comprador real.

La aceptación U1 incluye carrera de 20 clientes con N=4 y tres vivos, peso
agotado, espaciado, reducción de N, vencimiento durante admisión, JSON inválido
con fallback, censo malo con 75, boot nuevo, ambos bordes UTC, SIGKILL con hijo
vivo, CPUs disjuntas y siete hijos reales con la afinidad del recibo.
Los gates de Jale y Hasito, el handoff completo, mirrors, reportes y emisores
migrados se acreditan en sus respectivos repos antes de activar.

La implementación concentra solicitud y claim en un archivo, en lugar de dos
registros que podrían divergir tras SIGKILL. La última admisión se deriva de
esas claims duraderas. El censo legacy elige 75 en vez de reclamar por PID o
mtime. Son precisiones conservadoras del diseño base y se declaran en la
entrega U1. No equivalen a APTO ni a una integración ya desplegada.
