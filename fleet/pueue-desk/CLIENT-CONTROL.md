# Contrato local de clientes, CC0

`client_contract.py` define `pueue-client-control@1` para el futuro subcomando
local **`control.py client`**. Petición y respuesta usan ese único schema;
`op` y la dirección del intercambio distinguen sus formas. Es una biblioteca
pura Python 3.9/stdlib: no instala el subcomando ni cambia ningún escritor.
La compuerta **clientes coordinados sigue apagada hasta CC8**, por host.
`COORDINATED_GATE=False` es una declaración de esta entrega, no un ajuste
nuevo del motor. El corpus es `corpus/client-control-v1.json`, con contenedor
`pueue-client-corpus@1`; ese contenedor de pruebas no es otro protocolo.

Se adopta la alternativa (a): conservar ID y worker sin carga. No se publica
130 al confirmar stash, ni se cambia la regla 2 de C1. La autoridad del
lanzador conserva aceptación, resultado, recursos, espejo y procedencia;
PAO acredita exclusivamente sus efectos y reservas sobre la cola.

## Transporte y negociación positiva

La instalación fija el host, autoridad y daemon. Los clientes llaman por su
transporte existente al subcomando local, separado del canal `rack`, sin
listener ni clave nueva. Una petición no selecciona host, grupo, socket,
configuración, binario ni directorio. El panel sigue cancelando mediante el
controlador fijo del lanzador; no recibe acceso a `client`.

Un único objeto JSON UTF-8 por stdin/stdout, máximo **16 384 bytes** en cada
dirección. CC1 debe limitar la lectura a MAX_BYTES+1 antes de decodificar.
`parse` rechaza extras a toda profundidad, claves duplicadas (también con
escapes equivalentes), varios documentos, UTF-8 inválido, NaN/Infinity,
tipos inexactos y versiones desconocidas. `Invalid` sólo lleva un código
cerrado; no copiar el contenido que provocó el rechazo. `validate_request`
y `validate_response` sirven también para objetos ya decodificados, con el
mismo límite. Sólo `decode` preserva la detección de duplicados del wire.

La negociación comienza exactamente con:

```json
{"schema":"pueue-client-control@1","op":"capabilities"}
```

La respuesta global contiene exactamente `schema`, `op`, `version:1` (entero, nunca
booleano) y `ops`, la lista ordenada completa `OPS` del módulo. `negotiated`
exige esa respuesta válida y salida de protocolo 0. **No hay fallback raw**:
2, 64, timeout, ausencia del subcomando, otra ruta, salida desconocida o una
capacidad incompleta bloquean la mutación. Un código de salida no acredita
un host viejo. La versión positiva acredita el protocolo disponible, no el
filtro instalado ni el permiso de reordenar, ni capacidades por corrida.

Para obtener la identidad inicial sin crear una acción, capabilities admite
la forma cerrada adicional `{schema,op,launcher,ref}`. La respuesta añade
exactamente `launcher`, `ref`, `identity`, `reason`; devuelve la identidad
saneada de esa corrida, con generación/huella, o identity null y reason
not_found/unsupported/identity. La pareja se coteja, incluso frente a una
respuesta global. El cliente usa esa observación en expected; el host la
revalida bajo lock antes de actuar. La observación no reserva, acepta ni
acredita Cancelar del panel, y puede quedar obsoleta de inmediato. Así no se
exige que el cliente invente la generación ni se abre un selector de daemon.

La futura CLI sale 0 si entrega una respuesta válida de negocio (incluidos
rechazo/diferimiento/incertidumbre); 2 por petición inválida. Ante un cuerpo
que no permite recuperar identidad se devuelve el rechazo cerrado de
`invalid_request_response()`: schema común, `op:null`, `status:rejected`,
`effects:false`, `reservation:none`, `reason:invalid_request`. No se inventan
ref/action_id ni recibos. Este error sin atribución no cierra reservas ni
prueba ausencia de efectos de intentos anteriores. Su salida es 2; incluso
con salida 0 no sirve para negociar. Cualquier falta de respuesta válida
sigue siendo no confirmación; jamás permiso para una mutación raw.
Esos códigos de transporte no son el resultado de la corrida.

## Petición, identidad por fase y operaciones

Salvo las dos formas de capabilities, todas las peticiones tienen `schema`, `op`, `launcher`,
`ref`, `action_id`, `phase`. `launcher` es `remote-run`, `jale`, `apotheca` o
`vigilante`. `ref` y `action_id` son tokens de 8–64 caracteres ASCII:
`[A-Za-z0-9][A-Za-z0-9-]{7,63}`. No son rutas ni autorizaciones: el host los
resuelve mediante su adaptador fijo, comprueba pertenencia física e identidad
y deriva el grupo. Status añade nada más y lleva `phase:"action"`.

Las demás operaciones añaden exactamente `mode`, `expected`, `acceptance`,
`proof`; signal/signal-manual añaden también `signal`. Los dos últimos campos
son null salvo cuando corresponde evidencia. `mode` distingue `rack-queued`,
`rack-running`, `own`, `own-legacy`, `manual`. No se infiere soporte de panel
por grupo/etiqueta: apotheca sólo usa parada propia; vigilante sólo manual (signal-manual, finish y status).
Jale directo en Mini sigue fuera de Cancelar. `own-legacy` permite que un
adaptador fijo acredite la identidad anterior sin exigir retrospectivamente
un manifiesto nuevo, y nunca leyendo carga, metadatos de ejecución ni logs.
Si no puede acreditarla, conserva recursos y devuelve no confirmación.

Cada `expected`/`identity` contiene exactamente:

| Campo | Significado |
|---|---|
| `generation` | Digest hexadecimal de 64 caracteres de la generación acreditada del daemon; no la elige el cliente. |
| `task_id` | Entero exacto 0…2³¹−1, conservado durante toda cancelación C1/C2. |
| `created_at` | Fecha de creación normalizada de la tarea. |
| `fingerprint` | Huella opaca SHA-256 de identidad acreditada por el host; no publicar material de carga para construirla. |
| `attempt` | `enqueued_at` original del intento, guardado duraderamente antes de stash; no se recalcula desde Running. |
| `state` | Estado visible: Queued, Stashed, Running, Paused, Done; Locked y Absent son observaciones conservadoras. |
| `enqueued_at`, `enqueue_at`, `started_at` | Proyección explícita por estado, con null cuando el dato no existe. |

Todas las fechas usan UTC canónico `YYYY-MM-DDTHH:MM:SS.ffffffZ`; los adaptadores
normalizan el formato stock. `created_at <= attempt`. Esta normalización es
una representación del wire; C5 sigue guardando su instante original en el
lanzador y no reescribe su archivo ni su reloj.

| Estado | `enqueued_at` | `enqueue_at` | `started_at` |
|---|---|---|---|
| Queued | Igual a `attempt` | null | null |
| Stashed | **null** | null o programación stock | null |
| Running/Paused | Actual, >= `attempt` | null | Actual, >= `enqueued_at` |
| Done | Actual, >= `attempt` | null | Actual o null si no hubo nacimiento |
| Locked/Absent | null | null | null |

Stock admite stash de Queued **y Locked**; start por ID admite también Locked
y no respeta la pausa del grupo. Por ello **ninguna petición mutadora admite
Locked**, tampoco cancel, resume_queued, stop legacy o Jale verify. Locked
ajeno se rechaza/reintenta sin tocarlo; sólo la recuperación atribuida del
motor puede tratar su propio Locked. No se adopta un Stashed externo.

`transition` conserva generation/task_id/created_at/fingerprint/**attempt**.
Queued→Stashed pierde `enqueued_at`; Stashed→Running lo renueva, conservando
el intento por el diario de la misma acción. No se exige igualdad ingenua
del timestamp antiguo ni se trata Running/Done como permiso para repetir
start. Los cambios de timestamps de signal/finish se rechazan. Una generación,
ID o intento distinto necesita reconciliación, no un nuevo efecto automático.

| Operación | Estado esperado → observado tras efecto | Restricciones |
|---|---|---|
| `hold` | Queued → Stashed | Reserva durable **antes** de stash; revalidar después. Running da stale_state sin escalar a kill. |
| `cancel-no-payload` | Stashed → Running/Done | Sólo remote-run/Jale compatibles; aceptación enlazada, grupo abierto y sin cierre incompatible; start único, sin carga. Nuevo `enqueued_at` no anterior a aceptación. |
| `signal` | Running/Paused → mismo estado/Done | Sólo TERM/KILL, revalidación antes de cada señal; rack-running requiere aceptación. Gracia fuera de control.lock. |
| `signal-manual` | Running → Running/Done | Sólo vigilante/manual, clasificación manual e identidad releídas por el host. Sólo TERM/KILL. |
| `retire-held` | Stashed → Absent | Sólo own-legacy; nunca C1/C2 del panel. Intención de retirada anterior al efecto. |
| `retire-interrupted` | Done → Absent | Sólo parada propia, sin resultado normal previo; no retirar éxito/fallo publicado. |
| `finish` | Done → Done o Absent → Absent | Prueba durable enlazada; rack exige Done y queued exige worker/no-payload con nacimiento. Cierra la reserva; manual sólo cierra Done. |
| `status` | Proyección del diario | Recupera la MISMA acción/fase; no crea otra cancelación ni reenvía a ciegas. |

La `phase` coincide con `op`, excepto `signal:TERM`, `signal:KILL`,
`signal-manual:TERM`, `signal-manual:KILL` y status `action`. Es un conjunto
cerrado, no un identificador arbitrario. La clave durable es
`(launcher,ref,action_id,phase)`: mismo cuerpo da mismo recibo; otro cuerpo en
esa fase da idempotency_conflict, incluso tras reiniciar. El diario enlaza
todas las fases a la misma identidad inicial/modo/acción; permite las
transiciones explícitas de esta tabla, no cambiar el intento. TERM y KILL
son subpasos diferentes de una misma acción; un poll no genera UUID nuevo.
Los terminales no se podan con los logs. Un duplicado consulta el resultado
registrado, no vuelve a emitir RPC.

`acceptance` contiene exactamente `action_id`, `attempt`, `accepted_at`,
`origin` (null o `rack-panel`) y `receipt` (digest opaco). Se proyecta de la
primera aceptación durable del lanzador, con hora/origen juntos bajo su
candado de resultado: después de hold confirmado, antes de start/señal.
La intención anterior a stash no fija hora C5. Repetir o recuperar conserva
esa aceptación; el validador del par exige igualdad completa. Si otra acción
encuentra una aceptación vigente, recupera el action_id ganador; no reetiqueta
su aceptación para hacerla encajar con un identificador nuevo.

`proof` contiene exactamente `kind` (`no-payload`, `terminated`,
`interrupted`), `attempt`, `receipt`, `acceptance_receipt`, `exit` (entero
0…255). Las dos primeras pruebas requieren aceptación y salida 130; su
`acceptance_receipt` enlaza el digest de la aceptación. `interrupted` usa
null y sólo sirve para cierre propio acreditado, jamás Cancelar del panel.
Una cancelación probada conserva Done y nacimiento también en parada propia;
Absent sólo cierra una retirada/interrupción propia acreditada.
Una huella escrita por el solicitante **no es prueba suficiente**: CC1/CC2
resuelven y verifican los recibos mediante adaptadores fijos y comparan el
intento vigente. El esquema valida forma/enlaces, no autentica una afirmación
ni sustituye los candados, la historia segura o la prueba real del worker.

## Respuestas y progreso

Salvo capabilities y el error sintáctico sin atribución, la respuesta lleva exactamente `schema`, `op`, `launcher`,
`ref`, `action_id`, `phase`, `mode`, `status`, `effects`, `reservation`,
`identity`, `acceptance`, `proof`, `reason`. Los consumidores deben usar
`parse(raw,response=True,request=peticion)` para cotejar eco, fase, aceptación,
prueba y transición. Status toma modo y evidencia del diario original.

| Status | Efecto acreditado de la fase | Reserva / evidencia |
|---|---|---|
| `held` / `started` / `signalled` / `retired` | `effects:true` | `held`; estado posterior compatible; esto **no** significa cancelled. |
| `pending` | `effects:false` en esta consulta | `held`, identidad conocida y razón cerrada; falta progreso/prueba. |
| `deferred` | `effects:false`, **ningún RPC enviado por esta petición** | `none` si hold aún no ganó; `held` cuando ya existe. cancel-no-payload/finish conservan `held`. Sin nueva aceptación/terminal. |
| `unknown` | `effects:null`, pudo ocurrir | **held**, intención/reserva conservada, bloquea acciones incompatibles. Consultar, no repetir. |
| `rejected` | `effects:false` | `none` o reserva anterior `held`; subpetición rechazada. Nunca borra una reserva previa. |
| `finished` | `effects:false`: finish sólo cierra el diario | `closed` con prueba enlazada. No autoriza resucitar ni retirar el ID C1/C2. |

En un status se informa la fase ya acreditada; true no implica un RPC nuevo
durante esa consulta. `reason` es null en efectos confirmados y finished;
en los otros casos es un enum cerrado de `REASONS`, sin texto del daemon.
Un RPC enviado con respuesta perdida nunca se rebaja a deferred. Un hijo/CLI
sobreviviente mantiene unknown hasta recogerlo y acreditar el efecto o hasta
una recuperación supervisada del daemon que excluya mensajes antiguos.
Dos lecturas iguales ni un timeout prueban ausencia de efecto tardío.

## Orden de candados y recogida autónoma (CC1/CC2)

Un único inode estable de **control.lock por host**, compartido con motor,
guardia, preparador y goteo. No reemplazarlo ni heredar su autoridad mediante
un token al hijo. Orden de adquisición: candados propios de corrida/espejo,
resultado breve y **control.lock al final**. El lanzador libera result.lock o
result_control **antes de start del worker y de esperar su muerte**, porque
el worker necesita publicar bajo ese candado.

**Con control.lock tomado nunca se llama de vuelta a un lanzador, se espera
su result.lock ni se hace una llamada recursiva a client.** Tampoco se espera
la gracia de limpieza. La guardia sólo consulta su índice durable bajo lock.
Así se conservan también las lecturas de capabilities de Hasito #1107 bajo
result_control, sin introducir una inversión de locks.

La reserva persiste antes de toda RPC y entre hold, aceptación, start y
finish. Bloquea reorder y mutaciones incompatibles aunque el dueño muera y
flock quede libre. **Nunca expira por reloj**. Una reserva propia de cancelar
no impide la reapertura normal segura de guardia; de otro modo impediría su
propio start. No abre por encima de manual/presión/startup ni borra sus
razones. Una pausa manual que espera turno conserva después su intención.

El **reconciliador autónomo de acciones cliente de PAO (CC2)** recoge también
huérfanas, sin runner, waiter ni HTTP vivos: copia acción/reserva y
`generation`, revisión del diario y digest de petición **bajo control.lock**;
suelta el lock; obtiene evidencia por adaptador fijo; vuelve a tomarlo y
coteja las tres versiones antes de avanzar/cerrar. Si algo cambió, descarta
la propuesta y relee. Si falta prueba o queda RPC vivo, conserva unknown y
su razón. Cubre reserva antes de stash nunca enviado y resultado durable
publicado antes de finish. El preparador y las guardias ejecutarán ese ciclo;
no se delega su progreso al próximo usuario que consulte. CC0 sólo lo exige.

## Clientes viejos, filtro pendiente y elegibilidad

Antes de hablar con el daemon, CC7 rechazará toda mutación raw (stash,
start, enqueue, edit, señales, remove, pause, etc.), incluso si el llamador
se anuncia nuevo: salida **64** y exactamente:

> Pueue administrado: esta operación requiere el controlador coordinado; actualiza el lanzador

El rechazo no cambia fila/grupo, no registra argv ni acredita 130. Lecturas
sin efectos y add **ordinario** permanecen permitidos: ID nuevo, grupo
existente, sin prioridad, immediate/start ni alias/config que lo convierta en
otra operación. `-p`/`--print-task-id` sigue imprimiendo el ID. No se autorizan
mutaciones por bandera de entorno, hash, etiqueta ni nombre del padre.
Un status que recupera debe coordinar sus efectos. El filtro, inventario de
rutas efectivas, drenado de procesos previos, mantenimiento/relink y rollback
son CC7/CC8: ninguna comprobación de hash reemplaza la barrera preventiva.

Productor/instalado, copia por corrida y controlador fijo se acreditan por
separado. Base de Hasito corregida: pueue.py corresponde a **9dd63b81e**, con
#1117; remote-run corresponde a **8ad2a9b48** (también igual en aquella main).
No atribuir el pueue.py instalado a 8ad2a9b48 ni sobrescribir copias vivas.
C1/C2 anteriores compatibles pueden seguir por un adaptador fijo acreditado,
sin remarcarlas con versiones o capacidades retrospectivas. Una corrida vieja
sin compatibilidad probada conserva unsupported y su mensaje anterior.

Decisión de producto: las tareas añadidas a mano con add ordinario **no
bloquean por sí solas** reordenar su grupo ni adquieren capacidad Cancelar.
Siguen todas las validaciones del motor; un Stashed ajeno continúa rechazado.
Un add concurrente no se incorpora a un plan viejo e invalida undo estricto
según U3.6. Después de cambiar prioridades, «delante» se calcula por el orden
observado `(-priority,id)` o se omite si no se acredita. Nunca por ID solo,
ni se promete reanudación en segundos durante recuperación/presión.

## Matriz de Cancelar C1–C5

| Estado acreditado por el lanzador | Sesión que espera / panel | Recursos e ID |
|---|---|---|
| No ganó hold: reorder/lock impiden actuar | Pendiente (`cancelling`), sin 130 ni hora C5; si pudo enviarse, unknown. | Intención conservada, ninguna prueba fabricada. |
| Stashed y aceptada; manual/presión/startup impiden start | Pendiente/no confirmada según el plazo vigente de cada cliente (por ejemplo 75 en stop propio); no terminal anticipado. | Reserva e ID retenidos; no se fuerza apertura. |
| Worker sin carga del intento vigente, Done, recibo 130 enlazado | `cancelled`: remote-run Mini/desk devuelve 130 y «cancelada»; Jale desk/verify devuelve 130, `PUEUE_CANCELLED=1`, sin «fallida». | Mismo ID Done; finish cierra sólo con prueba. |
| Running aceptada, limpieza todavía viva | `cancelling`; si no se acredita fin, `cancel_unconfirmed`. | Candados, recursos, servidores/espejo conservados hasta prueba de fin. |
| Resultado publicado antes de aceptación | `already_finished`, conserva **0/7/124/130/143** y presentación previa. Un 130/143 aislado no prueba cancelación. | No sustituir resultado ni retirarlo. |
| Carrera panel Queued→Running o Locked ajeno | `rejected/stale_state`; nunca escalar a matar activa ni tocar Locked. | Preserva trabajo e identidad. |
| Efecto incierto, respuesta perdida, generación no reconciliada | `unknown`; consultar mismo action_id, no nueva cancelación ni 130. | Reserva y recursos conservados. |

C3/C4 mantienen sus canales, capacidades por corrida e idempotencia; no se
amplían Jale Mini, apotheca ni grupos sin lanzador reconocido. Si el plazo o
la falta de contacto impiden confirmar, se conserva el comportamiento previo
del cliente, no se inventan 143/125 ni cancelada.

**C5:** sólo una corrida que negoció soporte y una aceptación confirmada del
intento vigente con origen rack-panel añaden, una sola vez y en hora local
del lector, exactamente:

> Cancelada desde el panel del rack a las HH:MM; no la relances sin preguntar

El canal local del panel origina ese enum; nunca el navegador. Se transporta
el instante original en la entrega terminal junto al resultado enlazado; no
se reconstruye desde logs, un clic o un poll. Reintentos/recuperación no lo
cambian. Sin opt-in/origen, consumidor anterior o señal propia, se conserva
la presentación anterior. Un fallo opcional de procedencia después de prueba
terminal no cambia 130: muestra «No se pudo leer el origen o la hora de la
cancelación». Un fallo de transporte antes de la prueba no inventa terminal.

## Corpus y comprobación

Los adaptadores de Hasito, Jale y apotheca reutilizarán los mismos pares
request/response con un coordinador falso. `valid` contiene intercambios;
`invalid` fija regla, dirección y espécimen; `wait` es la matriz de resultados
existentes de C1–C5 (no un nuevo wire); `protocol_errors` contiene el rechazo sin atribución; `panel_notice` fija aceptación y hora
del lector. Son datos sintéticos sin comandos, rutas de usuario, entornos,
logs ni secretos. No comparar respuestas con status crudo del daemon.

`tests/test_client_contract.py` valida el corpus completo, límites, tipos,
Locked en cada entrada, transiciones, ausencia de fallback y matriz de espera.
`tests/mutate_client_contract.py` anula cada regla por separado en una copia
propia bajo /tmp usando el mismo intérprete; exige una única prueba roja por
**AssertionError**, nunca ERROR/importación ni timeout. La base debe estar
verde antes de mutar. También comprueba los códigos cerrados de rechazo;
algunas reglas redundantes detectan el mutante por cambiar ese diagnóstico.
Estas pruebas dirigidas no ejecutan daemon, SSH ni control.py. Ejecutar con
`TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1`, Python 3.9 y el 3.12 ya instalado:

```text
python -I -m unittest discover -s fleet/pueue-desk/tests -p test_client_contract.py -v
python -I fleet/pueue-desk/tests/mutate_client_contract.py
```

Quedan CC1 coordinador/diario, CC2 reconciliador, CC3 Hasito, CC4 apotheca,
CC5 Jale/verify, CC6 escritores PAO, CC7 filtro/mantenimiento y CC8 acreditación
cruzada con daemon privado. Sólo CC8, además de las otras puertas de U3.6,
puede habilitar la compuerta. Acreditar Mini nunca habilita desk/U3.7.
