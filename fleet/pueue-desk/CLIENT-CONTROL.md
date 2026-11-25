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
y deriva el grupo. Status conserva el cuerpo completo de la petición de la
fase guardada en el diario: `mode`, `expected`, `acceptance`, `proof` y, para
señales, `signal`. Usa `op:"status"` y añade sólo `action_op`, la operación
original (incluido finish), nunca capabilities/status. `phase` conserva la
fase original exacta, también TERM/KILL; ya no se usa una fase genérica action.
No se consulta una acción sin su identidad y evidencia originales.

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
normalizan el formato stock. El alta stock toma `enqueued_at` antes de
`created_at`: `attempt < created_at` es legítimo, incluso en Running/Done que
conservan el primer encolado. No se impone orden entre esos dos campos.
Cuando existe start, no precede a created_at ni a enqueued_at; el enqueued_at
actual no precede al attempt guardado. Esta normalización es una
representación del wire; C5 sigue guardando su instante original en el
lanzador y no reescribe su archivo ni su reloj.

| Estado | `enqueued_at` | `enqueue_at` | `started_at` |
|---|---|---|---|
| Queued | Igual a `attempt` | null | null |
| Stashed | **null** | null o programación stock | null |
| Running/Paused | Actual, >= `attempt` | null | Actual, >= `enqueued_at` |
| Done | Actual, >= `attempt` | null | Obligatorio, >= `enqueued_at` y `created_at` |
| Locked/Absent | null | null | null |

Stock admite stash de Queued **y Locked**; start por ID admite también Locked
y no respeta la pausa del grupo. Por ello **ninguna petición mutadora admite
Locked**, tampoco cancel, resume_queued, stop legacy o Jale verify. Locked
ajeno se rechaza/reintenta sin tocarlo; sólo la recuperación atribuida del
motor puede tratar su propio Locked. No se adopta un Stashed externo.
Con Pueue stock (CC3) cada RPC mutadora lleva **una sola id**: `stash` con
`enqueue_at` null, `start`, `kill` siempre con señal (`-s`, SigTerm o SigKill)
y `remove`. Nunca se usan `-a`, `--group`, selección mixta ni `kill` sin
señal. La versión del daemon se negocia en el saludo: la trama 2 debe ser
exactamente `0.31.1`, la versión de protocolo de pueue_lib que trae la
4.0.4, no el número de la versión del binario. Cualquier otra falla cerrada
antes de escribir la petición.
La observación de Stashed programado es válida, pero **no** una reserva:
toda respuesta con `reservation:held` exige `enqueue_at:null`, también
pending/deferred/unknown/rejected y su proyección status. Las peticiones
cancel-no-payload y retire-held también lo exigen en expected. De otro modo,
stock lo reencola automáticamente al vencer la programación, sin RPC.

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
| `finish` | Done → Done o Absent → Absent | Prueba durable enlazada; rack exige Done y queued exige prueba durable no-payload del worker. Cierra la reserva; manual sólo cierra Done. |
| `status` | Proyección del diario | Recupera la MISMA acción/fase; no crea otra cancelación ni reenvía a ciegas. |

La `phase` coincide con `op`, excepto `signal:TERM`, `signal:KILL`,
`signal-manual:TERM` y `signal-manual:KILL`. Status conserva la fase de
`action_op`, sin cambiarla ni combinar TERM/KILL. Es un conjunto
cerrado, no un identificador arbitrario. La clave durable es
`(launcher,ref,action_id,phase)`: mismo cuerpo da mismo recibo, salvo el reintento explícito de `deferred`
sin efecto definido en «Entrega CC2»; otro cuerpo en esa fase da
idempotency_conflict, incluso tras reiniciar. El diario enlaza
todas las fases a la misma identidad inicial/modo/acción; permite las
transiciones explícitas de esta tabla, no cambiar el intento. TERM y KILL
son subpasos diferentes de una misma acción; un poll no genera UUID nuevo.
Los terminales no se podan con los logs. Un duplicado consulta el resultado
registrado, no vuelve a emitir RPC.

`acceptance` contiene exactamente `action_id`, `attempt`, `accepted_at`,
`origin` (null o `rack-panel`) y `receipt` (digest opaco). Se proyecta de la
primera aceptación durable del lanzador, con hora/origen juntos bajo su
candado de resultado: después de hold confirmado, antes de start/señal.
Hold y cualquier proyección de su fase exigen `acceptance:null`; no se puede
presentar una aceptación previa al stash ni introducirla en held. La intención
anterior a stash no fija hora C5. Repetir o recuperar conserva
esa aceptación; el validador del par exige igualdad completa. Si otra acción
encuentra una aceptación vigente, recupera el action_id ganador; no reetiqueta
su aceptación para hacerla encajar con un identificador nuevo.

`proof` contiene exactamente `kind` (`no-payload`, `terminated`,
`interrupted`), `attempt`, `receipt`, `acceptance_receipt`, `exit` (entero
0…255). Las dos primeras pruebas requieren aceptación y salida 130; su
`acceptance_receipt` enlaza el digest de la aceptación. `interrupted` usa
null y sólo sirve para cierre propio acreditado, jamás Cancelar del panel.
Una cancelación probada conserva Done también en parada propia. Done siempre
lleva started_at en stock, incluso FailedToSpawn o DependencyFailed: ese campo
**no prueba nacimiento**. Sólo la prueba durable del worker acredita no-payload.
Para no-payload, tanto finish como status terminal exigen enqueued_at no anterior
a la aceptación, igual que started. Esa relación no se impone a terminated de
una tarea que ya estaba Running al aceptar.
Absent sólo cierra una retirada/interrupción propia acreditada y sus timestamps
son null: por ejemplo, una pendiente retirada sin arrancar. No se proyecta esa
retirada como un Done sin start.
Una huella escrita por el solicitante **no es prueba suficiente**: CC1/CC2
resuelven y verifican los recibos mediante adaptadores fijos y comparan el
intento vigente. El esquema valida forma/enlaces, no autentica una afirmación
ni sustituye los candados, la historia segura o la prueba real del worker.

## Respuestas y progreso

Salvo capabilities y el error sintáctico sin atribución, la respuesta lleva exactamente `schema`, `op`, `launcher`,
`ref`, `action_id`, `phase`, `mode`, `status`, `effects`, `reservation`,
`identity`, `acceptance`, `proof`, `reason`; status añade `action_op`.
Los consumidores deben usar
`parse(raw,response=True,request=peticion)` para cotejar eco, fase, aceptación,
prueba y transición. Status toma modo, operación y evidencia del diario
original: se quita únicamente `action_op` y se sustituye op por su valor.
El mismo `validate_request` valida la petición proyectada y el mismo
`validate_response` valida la respuesta proyectada frente a ella. No hay
una segunda lista de reglas para status: reserva, aceptación, prueba, actor,
resultado, transición y eco de la fase pasan por el validador directo.
La única comprobación adicional de la consulta es cotejar toda identidad
observada con expected (generación, ID, creación, huella e intento), incluso
cuando aún no hay efecto confirmado. Modo, operación y fase deben coincidir
con el diario por los mismos cotejos de eco que en la respuesta directa.
Una fase distinta requiere otra consulta con su expected guardado, no adoptar
la identidad que venga en una respuesta ajena.

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
Un RPC enviado con respuesta perdida nunca se rebaja a deferred. Con el
emisor stock (CC3), un emisor vivo mantiene unknown hasta publicar su sello;
un emisor muerto sin sello mantiene unknown hasta S4 (la vida del daemon del
plan también terminó). S3 y S4 liquidan sin atribuir. Un hijo/CLI anterior,
sin plan, conserva la regla previa: unknown hasta recogerlo y acreditar el
efecto o hasta una recuperación supervisada del daemon que excluya mensajes
antiguos. Dos lecturas iguales ni un timeout prueban ausencia de efecto
tardío: S4 se apoya en la muerte de una vida del daemon, no en lecturas.

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
Así se conservan también las lecturas de capabilities del lanzador bajo
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
separado, con la procedencia e inventario de lo instalado fuera del repositorio
público. No sobrescribir copias vivas.
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
| No ganó hold: reorder/lock impiden actuar | `unknown`, aún no aceptada, sin 130 ni hora C5. El coordinador puede confirmar deferred sin efecto, pero eso no afirma aceptación ante el panel. | Intención conservada, ninguna prueba fabricada. |
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
verde antes de mutar. Cada prueba aislada usa un espécimen que el mutante
admite; el arnés exige el diagnóstico exacto «La regla … admitió el caso
inválido». Un cambio de código de rechazo, excepción o fallo de otra regla
no cuenta. La pertenencia del estado se comprueba en phase, sin regla
redundante separada. La higiene del documento y del corpus rechaza referencias
a revisiones o PR de procedencia; los digests sintéticos repetidos del corpus
se reconocen como centinelas de prueba, no revisiones instaladas.
`tests/test_client_contract_r2.py` genera la paridad desde todos los negativos
de respuesta directa de operaciones mutadoras y todos sus pares válidos:
el status exige el mismo diagnóstico de rechazo y conserva los intercambios
válidos, con y sin cotejo de petición. Capabilities (negociación/observación)
y el error sintáctico sin atribución no son fases mutadoras consultables.
El arnés añade dos mutaciones de sitio: reducir held al resultado literal y
saltar el validador compartido de status; ambas mueren por su aserción exacta.
Estas pruebas dirigidas no ejecutan daemon, SSH ni control.py. Ejecutar con
`TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1`, Python 3.9 y el 3.12 ya instalado:

```text
python -I -m unittest discover -s fleet/pueue-desk/tests -p "test_client_contract*.py" -v
python -I fleet/pueue-desk/tests/mutate_client_contract.py
```

Quedan CC1 coordinador/diario, CC2 reconciliador, CC3 Hasito, CC4 apotheca,
CC5 Jale/verify, CC6 escritores PAO, CC7 filtro/mantenimiento y CC8 acreditación
cruzada con daemon privado. Sólo CC8, además de las otras puertas de U3.6,
puede habilitar la compuerta. Acreditar Mini nunca habilita desk/U3.7.

## Entrega CC1: diario y exclusión, sin habilitar reordenar

`control.py client` implementa el transporte local del contrato. No admite
argumentos ni selectores: la instalación elige host, autoridad, configuración,
entorno y binario. Sólo corre en la Mini. En el desk `client` sigue fallando con un diagnóstico,
no escribe el diario y la vista anuncia `capabilities.client:false`. Lee como máximo `MAX_BYTES+1` antes de decodificar; usa
los validadores CC0 tanto al entrar como al responder. Los rechazos de negocio
salen 0; una petición inválida sale 2 con `invalid_request_response()`.

`client_actions.py` conserva fases y reservas en tablas aditivas de la
SQLite ya adoptada, con `synchronous=FULL` y `fullfsync`. No crea otra autoridad
ni reemplaza el inode de `control.lock`. Cada clave de fase conserva petición,
digest, revisión y recibo; una repetición no reenvía RPC y otro cuerpo da
conflicto, incluso después de reiniciar. `status` proyecta ese recibo sin
recoger evidencia ni enviar efectos. Los terminales se conservan.

La reserva añade `hold_identity`: null hasta que el `hold` de esa misma
`(launcher, ref, action_id)` confirma, sin RPC incierta, la transición al
Stashed no programado. Guarda la identidad completa posterior, incluido el
intento original, en la misma transacción que el recibo `held`. Una reserva
abierta, un rechazo por carrera o ver Stashed después de perder la respuesta
no acreditan esa atribución. `cancel-no-payload` y `retire-held` exigen que
`hold_identity` coincida con el Stashed actual; si falta o difiere, rechazan
con `evidence_missing`, conservan la reserva y no envían RPC. Un diario
anterior sin el campo tampoco acredita hold. Sólo una recuperación atribuida
de CC2 podrá completar esa evidencia; no basta releer el estado del daemon.
Con Pueue stock, «sin RPC incierta» significa un sello `replied` del emisor,
y confirmar el hold exige, en CC1 y en CC2, las cuatro condiciones de
`rpc_verdict`: R+ (respuesta Success en la conexión del emisor ligada al
plan), P (lectura previa = `expected` en la vida D del plan), Q (lectura
posterior hecha después de ese sello, en la misma vida D, con Stashed sin
programar) y Ex (perfil de exclusión `filtered`). El código y la arista solos
no bastan. Antes de P1, CC1 confirmaba `held` con rc 0 más la arista; desde
CC3-P1 `client_actions.py` sólo confirma con `rpc_verdict` (véase «Entrega
CC3-P1»).

Con `control.lock` ocupado, una fase sin recibo responde `deferred` y
`lock_busy` sin RPC; si la acción ya tiene una reserva abierta, la respuesta
la declara `held`.

Antes de crear el hijo se confirma intención `unknown` y reserva. El hijo
espera en stdin hasta confirmar también su PID y nacimiento; no hereda el
candado. Toda respuesta perdida, caída o incertidumbre conserva la reserva.
Matar y recoger nuestro propio hijo tras su plazo no excluye mensajes ya
recibidos por el daemon. Ni el reloj, ni dos lecturas iguales, ni la muerte
del dueño cierran la acción. Una fase posterior tampoco atraviesa esa
incertidumbre.

Con Pueue stock, el hijo es el emisor `client_rpc.py` (P1). La intención y la
reserva FULL siguen antes de todo envío. Bajo `control.lock`, CC1 crea el
emisor esperando en stdin, lee su nacimiento, publica el plan y registra
`process={pid, birth, dispatch, plan_digest, state:'gated'}` antes de abrir la
compuerta; después escribe `dispatch` en stdin y pasa a `state:'sent'`. Espera
**el sello, no la salida del proceso**, como máximo 1 s. **No mata ni recoge
al emisor**: matarlo no excluía un mensaje ya leído por el daemon y destruiría
al único testigo. El emisor no hereda el candado. Sin sello, CC1 responde
`unknown`/`rpc_unresolved` y CC2 recoge el sello después.

El segundo acota **sólo la espera del sello**, no el turno de CC1 bajo
`control.lock`. Ese turno suma la lectura previa P, la relectura posterior a
la reserva, el estado del grupo en `start`, la espera del sello, la lectura
posterior Q y las escrituras FULL del diario. En P1 ninguna espera al daemon
queda sin plazo: cada lectura de estado del turno lleva el plazo de lectura
del colector de CC2, la mitad de sus 0,4 s, fijado en `Pueue.timeout` y
`Pueue.deadline`. Antes de P1 el lector por omisión esperaba sin plazo. Q tiene ese
plazo propio y no consume el segundo del sello. Las escrituras FULL con
`fullfsync` y la creación del emisor no tienen cota, igual que hoy y que en
CC2. Si vence P o la relectura, no se envía nada. Q sólo se exige para
atribuir un `success`. Si llega el sello y Q vence o falla, CC1 aplica
`rpc_verdict` con `post=None`: un sello `not-sent` (S2), un `failure` (R−) o
un `closed` (S3) liquidan o acreditan el rechazo como dice la tabla de
veredictos, sin esperar otra Q. Un `success` sin Q no atribuye: la fase queda
`unknown` con `evidence_missing`, no fija `hold_identity` y la reserva se
retiene a la espera de una Q posterior. Con un recibo no final, sea sin
sello o con un `success` sin Q, CC1 deja el recibo disponible y suelta el
candado. Lo recoge el siguiente ciclo de CC2 que alcance la reserva: lee
sello, plan, claim y una Q nueva fuera del candado, en sus lotes de ocho y
dentro de los 0,4 s que comparte el lote, y retiene con `evidence_missing` si
el plazo vence. Aplica `rpc_verdict` con los cotejos de revisión de CC2. Una Q
leída después del sello y en la vida D vale aunque llegue en otro turno; una
Q incompatible se vuelve a intentar cuando el índice alcance otra vez la
acción, con la garantía N/N+1 de la entrega CC2. No se promete progreso
mientras la guardia no obtenga su turno.

Una reserva impide reordenar y lo explica en `reorder_groups`; un reordenado
abierto o en recuperación difiere las acciones cliente sin RPC. La guardia
Mini puede reabrir normalmente con una reserva de cancelación conocida cuando
cede la presión; una RPC incierta bloquea esa apertura y conserva la razón
`pressure`, sin convertirla en pausa manual. Cancelar nunca abre
por encima de manual, presión o arranque. La poda conserva las tareas que
necesita una reserva abierta. No se cambian cuotas, modo ni política de cola.

El controlador también funciona si la instalación sólo publicó `control.py`
y falta `client_actions.py` o `client_contract.py`. Sin tablas cliente mantiene
las operaciones del host. Si existe el diario cliente, la vista permanece
válida y explica la reserva pendiente, reordenar queda cerrado en todos los
grupos, la poda se abstiene y las aperturas se retienen conservadoramente.
No se borra ni convierte el diario ni se reemplaza `control.lock`. La vista
del host anuncia `capabilities.client:false` si falta cualquiera de las dos
piezas o `editor_prioridad.py`, que la CLI necesita para identificar a sus
hijos; invocar `client` entonces falla con un diagnóstico de instalación,
sin negociación positiva ni acceso al daemon.

Son cuatro acreditaciones distintas:

1. **Protocolo instalado.** Con ambos módulos presentes se puede negociar la
   versión global del protocolo; eso no acredita una corrida.
2. **Soporte por corrida.** Los cuatro adaptadores instalados (`remote-run`,
   `jale`, `apotheca`, `vigilante`) devuelven hoy `unsupported`. Los formatos
   actuales no publican un vínculo acreditado entre launcher/ref y la
   generación e identidad originales de tarea/intento, o no permiten acreditar
   su clasificación manual. Un ID almacenado puede referirse a otra tarea
   tras reconstruir la cola; cotejarlo con la tarea actual no demuestra
   pertenencia. Los productores deben publicar el vínculo desde el alta
   verificada y contemplar caídas, relevo, reencolado y nueva generación.
3. **Filtro y acreditación del host.** CC7 debe interceptar las mutaciones raw
   antes del daemon; CC8 debe acreditar cobertura, drenado, recuperación y
   compatibilidad efectivas antes de habilitar reordenar. Ninguna de las dos
   acreditaciones anteriores demuestra esa exclusión preventiva.
4. **Perfil de exclusión.** `coordinated` o `filtered`, fijado por la
   instalación de CC7 en cada host y nunca por el cable ni por el solicitante.
   La atribución con Pueue stock exige `filtered`. Sin ese perfil, las fases
   mutadoras de `remote-run` responden `unsupported` en la capacidad por
   corrida. Esta acreditación no habilita `coordinated_reorder`.

Ni manifiesto, grupo, etiqueta, cwd ni un recibo declarado por el solicitante
bastan para sustituir la pertenencia o la clasificación acreditadas.
No se leen comandos, carga, metadatos de ejecución ni logs para habilitarlos.
La interfaz interna de adaptadores se prueba con evidencia sintética; las
entregas posteriores deben acreditar formatos fijos que permitan verificar
identidad, compatibilidad del worker, aceptación y prueba sin esos accesos.
La negociación global del protocolo no anuncia soporte por corrida.

CC2 recoge bajo `control.lock` acción, reserva, generación, ambas
revisiones y digest; recoge evidencia fuera del candado y vuelve a cotejar
las versiones antes de avanzar o cerrar. Recoge también intenciones
anteriores al envío, hijos identificados, recibos diferidos y resultados
publicados antes de `finish`, sin depender de runner, waiter ni HTTP vivos.
CC1 conserva esos datos; la entrega CC2 siguiente implementa la recogida.
CC6 entregará los archivos y el binario interno mediante los instaladores.
**`coordinated_reorder` sigue en `False`; sólo CC8 puede acreditar el corte.**


## Entrega CC2

`Reconciler` en `client_actions.py` recorre las reservas abiertas sin runner,
waiter ni HTTP. La guardia solicita el ciclo en su tick, incluso si el relevo
mantiene la admisión cerrada. `locked` conserva su descriptor durante todo el
trabajo protector del llamador. Al terminar ese trabajo, **todavía bajo el mismo
candado**, copia hasta ocho acciones y aplica pruebas del diario o evidencia
recogida entre turnos. Sólo después de cerrar el descriptor recoge nueva
evidencia en un proceso independiente y prueba una aplicación no bloqueante.
Si una aplicación deja la reserva abierta, renueva su copia y vuelve a recoger
en ese mismo ciclo exterior, incluso tras una retención idéntica o una fase ya
confirmada sin `finish`. Una propuesta cacheada permite revisar bajo contención;
nunca sustituye la recogida nueva de una acción abierta alcanzada por el lote.
Nunca se cede ni se recupera un candado prestado. La pausa protectora de la
guardia ocurre antes y no depende del reconciliador.

Garantía de progreso: si la guardia obtiene su turno en el tick N, cada acción
abierta alcanzada por su lote se revisa y avanza o retiene con motivo a más
tardar en N+1, aunque choquen todas las adquisiciones independientes. Los
cierres acreditables sólo con el diario se aplican en N. Las otras copias
conservan su posición del índice hasta su revisión: ni siquiera un lote de
más de ocho abiertas desplaza su obligación al copiar. La recogida termina
con prueba o con una propuesta de retención acotada. Si la aplicación propia
choca, N+1 aplica esa propuesta en el turno de la guardia, después de proteger.
No se promete progreso mientras la guardia no obtenga su turno. Cambios de
fase/autoridad invalidan la copia: se descarta y se recoge la versión vigente;
la reserva nunca se libera para resolver contención o falta de prueba.

La evidencia entre ticks se publica atómicamente fuera de `control.lock` en
`client-evidence/`, fuera de la autoridad SQLite y sin obligación de fsync.
Cada archivo liga acción/fase, PID/nacimiento, generación durable y observada,
revisión/digest de fase y reserva; su nombre deriva del vínculo completo, de
modo que un colector atrasado no pisa evidencia de otra versión. Se leen sólo
los archivos exactos del lote, con tamaño limitado; no se escanea el historial.
Una publicación interrumpida o un archivo ausente/corrupto obliga a recoger de
nuevo, nunca certifica un efecto. El núcleo coteja de nuevo todo el vínculo
bajo el candado al aplicar. Los archivos antiguos son descartables y no se
reutilizan para otra revisión; no son reservas ni recibos idempotentes.

`prepare_start` sigue solicitando el ciclo al salir de su ámbito protegido;
esto cubre su llamada directa y la CLI `preparar`. El ensayo no recoge ni
escribe. Como exec no desenrolla contextos, el wrapper de launchd ejecuta
explícitamente un ciclo **antes** de entrar a su propia sección de preparación.
Ese ciclo previo nunca hace fallar el arranque por contención. El arranque
**espera su propio control.lock como antes de CC2**: al quedar libre prepara y
ejecuta el daemon una sola vez. Esa espera preexistente no tiene un nuevo
plazo de rechazo. Después de adquirirlo relee la autoridad del relevo y vuelve
a comprobarla antes de exec. La preparación conserva el Stashed cliente,
incertidumbre y reservas a través del reinicio; nunca los adopta como propiedad
del editor de orden.

El ciclo independiente conserva sus adquisiciones no bloqueantes para copia y
aplicación. Antes de guardar coteja generación durable y observada **por cada
acción**, revisión/digest/proceso de la fase y revisión/digest/fase/apertura de
la reserva. En la Mini, la copia lee sólo PID/nacimiento nativo y firma del pid
file, ligados a la generación durable pendiente de autenticar. El colector,
fuera del candado y dentro de su plazo, autentica la generación con el `ps`
acotado entre dos cotejos de esa identidad nativa. Cada aplicación vuelve a
leerla por acción. Así detecta también la muerte o sustitución entre elementos
del lote, sin lanzar ni esperar procesos en la copia ni en la aplicación del
turno protegido. Las demás operaciones conservan su `ps` acotado. El desk usa
su identidad nativa de `/proc`. No hay RPC, callback al lanzador, result.lock,
espera de gracia ni llamada recursiva a client en esa aplicación.

Con `client_open` vacío no se crea colector ni se lee generación. Un único
proceso tiene plazo de 0,4 s para todo el lote; el RPC de lectura tiene un plazo
menor. Se recoge el proceso y su grupo privado al salir. Una lectura lenta o
sin prueba retiene con evidence_missing; la guardia ya ejecutó su protección.
El trabajo protegido está limitado por ocho copias/aplicaciones y sus lecturas
locales; SQLite, serialización, generación y disco añaden coste medido. Los
0,4 s acotan la recogida, **no el tick completo** ni la espera preexistente del
candado. No hay una cota universal de extremo a extremo independiente del
dueño anterior o de fallos del sistema de archivos.

El rechazo de arranque conserva el diagnóstico saneado de U3.5 y la rotación
acotada en `arranque.log`, bajo el candado independiente `arranque.log.lock`.
No espera `control.lock` para registrar. Si otro escritor del diagnóstico
retiene su candado más de 0,1 s, o falla el acceso al disco, se omite ese registro
y el arranque sigue rechazado; un log descartable no alarga la espera de autoridad.

Qué puede cerrar hoy:

- Una intención `unknown` anterior a publicar la identidad de cualquier hijo
  prueba que no se abrió la compuerta del RPC: CC1 confirma PID/nacimiento
  antes de abrir stdin. El hijo no registrado sólo puede recibir EOF. Si es
  el hold inicial, sin aceptación ni hold atribuido, se cierra la reserva con
  rechazo sin efecto. El cliente puede iniciar otra acción; no hubo aceptación
  C5. En otra fase se conserva la reserva y se devuelve `deferred`, sin efecto.
- Un hold rechazado en la segunda lectura (Locked o Stashed ajeno) también
  cierra sin cliente cuando el diario prueba que no lanzó hijo, no hay
  incertidumbre, aceptación ni hold atribuido y todas las fases de esa acción
  son rechazos/diferidas sin envío ni efecto. Sólo cambia la reserva y su
  índice: el recibo rejected, digest y revisión permanecen idénticos. No se
  atribuye el Stashed ajeno. Una fase previa con efecto impide este cierre.
- Una respuesta con efecto ya confirmada permanece histórica e idempotente.
  Con adaptador acreditado, una prueba durable enlazada permite añadir la
  fase finish y cerrar; se reutilizan los validadores de `client_contract.py`.
  No se sustituye held/started por un terminal de otra fase.

Los cuatro adaptadores instalados siguen devolviendo `unsupported`, sin
leer formatos productivos ni consultar el daemon para reconstruir pertenencia.
Con ellos, una identidad de hijo `gated`, `sent` o `reaped`, una aceptación
pendiente o un resultado supuesto conservan la reserva y su motivo cerrado.
La salida del hijo, el reloj y dos observaciones iguales nunca acreditan
exclusión de mensajes tardíos. Un hijo todavía vivo con el mismo nacimiento
retiene `rpc_unresolved`; un cambio de generación retiene `startup`.
Una reserva conocida conserva su recibo y su condición conocida; la falta de
nueva prueba no fabrica incertidumbre de un RPC que nunca se envió.
Con Pueue stock, los adaptadores siguen en `unsupported` hasta P2 y el perfil
`filtered`. Un sello válido permite recoger aunque el emisor siga vivo: «hijo
vivo → `rpc_unresolved`» pasa a ser «emisor vivo **sin sello**». R− (Failure
de una sola id) cierra el hold inicial sin aceptación ni `hold_identity`,
junto al rechazo sin envío. S3 y S4 ponen `uncertain:false`, sin cerrar la
reserva: sólo reabren las aperturas del grupo. Los hijos anteriores sin plan
conservan la regla de este párrafo.

**Liquidación y efecto de una fase de cola (perfil stock).** Desde P1,
`settled` sale sólo de S1–S4 y `effect_applied` sale sólo de `rpc_verdict`
(CC3) sobre el plan, el claim y el sello originales del emisor, más la
lectura previa P, la posterior Q y el perfil de exclusión. Se admite como
testigo la respuesta original **en la conexión del emisor ligada al plan**, y
sólo atribuye con exclusión acreditada (`filtered`). Se sigue prohibiendo
derivar el efecto del código o la salida de un CLI, de la muerte de un
proceso, de dos lecturas iguales o de la petición del cliente. Desde P1, el
adaptador del lanzador no aporta efecto a una fase del emisor: sólo da la
observación de Q y `finish`, y el núcleo ignora su `settled` y su
`effect_applied`. collect y apply usan `execution_link` más `rpc_verdict`.
Sólo los hijos anteriores sin plan conservan `settled` del adaptador y
`effect_applied` cotejado con `effect_matches`. `settled` no demuestra quién
produjo el estado observado.
Tras un gated incierto, incluso un Stashed con la identidad esperada más
settled conserva `unknown`, reserva abierta y `hold_identity:null` si
`rpc_verdict` no atribuye. cancel-no-payload y retire-held rechazan sin RPC
hasta acreditar la atribución; no basta excluir mensajes tardíos. La
sección «Emisor stock y perfil de exclusión (CC3)» fija los originales, las
reglas S1–S4 y la tabla del veredicto.
Las pruebas sintéticas recorren hold atribuido y resultado durable hasta
finish, sin depender de clientes vivos. No se reenvía stash/start/señal/remove
para resolver una respuesta perdida. Sin aceptación/prueba, se retiene; no
se inicia automáticamente un worker ni se fabrica 130.

La vista por grupo y capabilities transportan motivos fijos de retención,
validados por el sanitizador del rack: «una RPC del cliente puede seguir en
curso» (`rpc_unresolved`), «el lanzador aún no publica evidencia acreditada»
(`unsupported`) y «falta evidencia para cerrarla» (`evidence_missing` y demás
pruebas inconclusas). El diario conserva `reconcile_reason` como enum cerrado;
ningún texto del daemon se publica. El bloqueo de guardia/goteo usa la causa
existente `reason`, que atraviesa validate_guard y llega al panel.
Guardia y goteo `_one` del desk consultan las mismas reservas: una incierta
retiene la apertura y conserva pressure. Una reserva conocida de cancelación
permite la reapertura normal cuando cede presión, para evitar impedir su
propio start. Manual, presión, startup y `fence_reason` mantienen su autoridad.
El desk sigue rechazando `client` y anunciando `client:false` hasta U3.7.

### Reintento de deferred

CC2 precisa una única excepción temporal a «mismo cuerpo, mismo recibo»:
una petición mutadora repetida con **la misma clave y digest**, cuyo último
recibo está en `client_contract.REEVALUATED` (sólo `deferred`), se vuelve a
validar bajo control.lock. Deferred garantiza
que esa petición no envió RPC. Se conserva la revisión creciente y se aplican
otra vez identidad, primera aceptación, reservas y cierres. `status` sólo
consulta el recibo y nunca reintenta. Un cuerpo distinto sigue dando conflicto.

Una vez confirmado un efecto, el duplicado devuelve siempre su recibo sin
RPC, incluso tras reiniciar o cerrar la reserva con finish. Rejected, unknown
y finished tampoco se reevalúan por este mecanismo. No se adopta una política
general de reevaluación. Esto permite reintentar cancel-no-payload después de
una pausa manual sin cambiar action_id ni perder el hold atribuido y la primera
aceptación. Una respuesta perdida tras enviar siempre sigue siendo unknown.
El esquema del wire y sus validadores no cambian: cada petición y respuesta
continúan pasando por CC0; la prueba temporal y su mutación viven en CC2.

### Índice y conservación

`client_open` indexa sólo las reservas abiertas y rota por último cotejo.
El índice se actualiza en la misma transacción que la reserva. `client_scan`
migra diarios CC1 en lotes de ocho filas; mientras está incompleto, todas las
aperturas/reordenados se retienen y la poda se abstiene. No se escanea el
historial completo en un tick, ni se archivan/borran claves, digests o recibos.
Una reserva nunca vence por reloj y un terminal nunca se resucita. Una
retención idéntica no reescribe recibo/reserva ni incrementa sus revisiones;
el índice avanza de todos modos para no perder el turno de otras acciones.

CC3–CC5 deben entregar los adaptadores acreditados; CC6 los instaladores y
escritores restantes; CC7 el filtro preventivo y CC8 la acreditación cruzada.
**La compuerta `coordinated_reorder` sigue apagada.** No se cambian cuotas,
modo ni la tabla de la Mini; la acreditación del desk sigue siendo U3.7.

## Emisor stock y perfil de exclusión (CC3)

Pueue 4.0.4 queda sin modificar. El daemon stock no produce evento de
efecto, así que PAO lo reconstruye con originales propios y una función pura.
Lo indistinguible es `unknown` y se retiene: ninguna reserva se libera sin
prueba positiva. Las corridas antiguas sin evidencia son `unsupported`, una
reserva incierta se retiene y no existe un camino raw. `remote-run` se activa
en cada host junto con CC7, con el perfil `filtered`; hasta entonces sigue
inerte (`unsupported`).

### Originales de una entrega

El emisor `client_rpc.py` (P1) es un proceso de un solo uso que sólo escribe
en `A/client-rpc/<dispatch>/`, donde `A` es el directorio de autoridad. Cada
original es JSON cerrado, publicado con temporal 0600, fsync, `link`
exclusivo y fsync del directorio. Se rechazan extras, duplicados, tipos
aproximados y tamaños por encima de 16 384 bytes; el plan admite 32 768 con
el mismo decodificador estricto y límite explícito. `decode` y `MAX_BYTES`
del cable no cambian.

| Schema | Escritor | Campos |
|---|---|---|
| `pao-client-rpc-plan@1` | CC1, bajo `control.lock`, antes de abrir la compuerta | `schema, dispatch, request, group, verb, task_id, daemon:{pid, birth, generation}, pid, birth` |
| `pao-client-rpc-claim@1` | El emisor, antes de conectar | `schema, dispatch, plan_digest, pid, birth` |
| `pao-client-rpc-seal@1` | Sólo el ganador del claim | `schema, dispatch, plan_digest, pid, birth, peer, outcome, reply, reason, receipt` |

`request` es la petición CC0 exacta de la fase. `verb` sale del mapa cerrado
`RPC_VERBS` (`stash`, `start`, `kill:SigTerm`, `kill:SigKill`, `remove`) y
`task_id` es el de `expected`. `daemon` es la vida D de la lectura previa, con
la generación de `expected`. `pid` y `birth` son los del emisor; el nacimiento
es nativo (`darwin:<s>:<µs>` o `linux:<boot_id>:<ticks>`). `receipt` es
H(sello sin `receipt`), con H el SHA-256 del JSON canónico del diario. El
sello no guarda el texto de la respuesta, rutas, argv ni entorno.

| `outcome` | Significado | `peer` | `reply` | `reason` |
|---|---|---|---|---|
| `not-sent` | No se escribió ningún byte de la trama 3 | null o D | null | `startup`, `unsupported` o `evidence_missing` |
| `replied` | Llegó la trama 4 completa | D | `success`, `failure` o `unrecognized` | null |
| `closed` | Se escribió la petición y el daemon cerró sin trama 4 | D | null | null |

`closed` exige que el **daemon** haya cerrado o reiniciado la conexión: fin de
archivo o reinicio en la lectura de la trama 4. Si el propio emisor aborta
después de escribir la trama 3, por un error local o una señal que no ignora,
no sella `closed` ni ningún otro resultado. El handler del daemon puede
seguir vivo y aplicar la petición después. La entrega queda como emisor sin
sello: retiene hasta S4.

No hay documento `effect`. La atribución la calcula `rpc_verdict` y el diario
la guarda junto al recibo como `attribution = {dispatch, seal_receipt,
exclusion}` (P1).

### Escritores, candados y restauración

- **Sólo el ganador durable del claim sella, también en negativo.** El claim
  se publica con `link` exclusivo antes de conectar; el perdedor sale sin
  conectar y sin sello. `execution_link` exige el mismo pid y nacimiento en
  plan, `process`, claim y sello, y que `peer`, cuando existe, sea la vida D
  del plan. No existe sello por `lock_busy`: con `control.lock` ocupado, CC1
  responde `deferred` antes de crear plan o emisor.
- **Orden de `control.lock` frente al mutex del daemon.** El orden es único:
  candados del lanzador → `control.lock` (CC1) → mutex de estado del daemon,
  que sólo toma el daemon dentro de cada handler. El emisor no toma candados.
  PAO nunca espera algo que el daemon necesite para terminar y el daemon nunca
  necesita `control.lock`.

  | Verbo | Bajo el mutex del daemon | Punto de efecto | Después de responder |
  |---|---|---|---|
  | `stash` | Pasa a Stashed, sin guardar | Antes de la respuesta | Nada |
  | `start` | Crea el proceso, los logs y guarda | Antes de la respuesta | El worker vive con la tarea, no con la RPC; un pánico al crear logs tumba el daemon (S3 o S4) |
  | `kill -s` | Señal al grupo de procesos | Antes de la respuesta | El paso a Done llega después desde el gestor de tareas; `kill` sin señal está prohibido |
  | `remove` | Quita y guarda | Antes de la respuesta | Nada |

  Ningún verbo devuelve una llamada terminal a PAO. La guardia y el
  preparador esperan `control.lock` mientras dure el turno de CC1, como hoy.
  Ese turno no espera al daemon sin plazo: el sello espera a lo sumo 1 s, y P,
  la relectura, el estado del grupo y Q tienen cada una su plazo de lectura.
  Las escrituras FULL del diario no tienen cota, así que no hay una cota de
  extremo a extremo del turno; su reparto está en «Entrega CC1».
- **Ancla de `client-add` y restauración.** El ancla
  `prepare_client_binding→save_id→publish_client_binding` y la recuperación
  previa al ID son de Hasito y no cambian con stock. Los directorios
  `client-rpc/<dispatch>/` son lápidas: no se borran mientras exista el
  registro que los nombra. Sin plan o con un sello sin claim, el veredicto es
  `evidence_missing` y se retiene sin liquidar. Sin claim con el emisor
  muerto, el motivo también es `evidence_missing`: retiene mientras viva D y
  liquida por S4, sin atribuir, cuando D termina. En todos esos casos la
  reserva se retiene, nunca es `not_sent` y nunca hay reenvío. Una
  restauración que conserva la SQLite y pierde `client-rpc/`, o sólo el
  claim, sólo produce retenciones de la reserva, y un emisor anterior no
  alcanza una vida nueva del daemon porque coteja el par antes de enviar.
  Así se cumple §2.7.3 del alcance stock, la corrección del juicio sobre la
  restauración de claims: motivo `evidence_missing`, reserva retenida y nunca
  `not_sent` ni reenvío.
- **Claim perdido frente a claim pendiente.** El dato que los distingue es la
  vida del emisor que nombran el plan y `process`, por pid y nacimiento: el
  `emitter_alive` que el colector ya calcula. Sólo ese emisor puede publicar
  el claim, y lo hace antes de conectar. Mientras vive, la falta de claim es
  «aún no publicado» y retiene `rpc_unresolved`. Una vez muerto, el claim ya
  no puede aparecer. Su falta es entonces indistinguible entre un emisor
  muerto antes de publicarlo y un claim perdido en una restauración. Mientras
  vive D retiene `evidence_missing` sin liquidar ni quitar `uncertain`. Cuando
  D termina, S4 vale igual que con claim: el emisor muerto no alcanza otra
  vida y nadie lee el búfer de una vida muerta, así que liquida sin atribuir,
  con `uncertain:false`, motivo `evidence_missing` y la reserva retenida. Para
  distinguir los dos casos no sirven otros datos:
  - `process.state` no certifica nada: `gated` no prueba que stdin siguiera
    cerrado.
  - Una marca de restauración depende de que la restauración se declare. Una
    pérdida accidental no la escribe, y el juicio exige fallar cerrado ante
    una restauración incoherente.

### Liquidación y veredicto

`not_sent` es seguro sólo con N1 (el diario no tiene `process`) o N2 (sello
`not-sent` del dueño del claim). `settled` positivo:

- **S1.** Sello `replied`, de cualquier clase: el handler terminó.
- **S2.** Sello `not-sent`.
- **S3.** Sello `closed`. Un cierre del daemon implica que la tarea de esa
  conexión terminó (E6), viva o no la vida `peer`.
- **S4.** Sin sello, emisor muerto **y** la vida D del plan terminada, con o
  sin claim. Con el claim del dueño el motivo es `startup`; sin claim es
  `evidence_missing`, porque pudo perderse en una restauración.

S3 y S4 liquidan sin atribuir: la reserva pasa a `uncertain:false` y sigue
abierta. Un emisor vivo sin sello, con o sin claim, o un emisor muerto con
claim y D viva retienen `rpc_unresolved`. Un emisor muerto sin claim y con D
viva retiene `evidence_missing`.

`rpc_verdict(record, plan, claim, seal, emitter_alive, life_alive, pre, post,
exclusion)` devuelve `(settled, effect, reason)`. Los booleanos de vida los
calcula el colector; banderas, perfil, P o Q mal formados son un `Invalid`
que el llamador retiene como `evidence_missing`.

| Historia | Veredicto |
|---|---|
| Plan ausente o enlace roto (no dueño, también en un sello negativo; `peer` de otra vida, sello sin claim, P distinta de `expected` o de D) | `(false, null, evidence_missing)` |
| Sin sello ni claim, emisor muerto (claim perdido o nunca publicado) y D viva | `(false, null, evidence_missing)` |
| Sin sello ni claim, emisor muerto y D terminada (S4) | `(true, null, evidence_missing)` |
| Sin sello ni claim, emisor vivo (claim aún no publicado) | `(false, null, rpc_unresolved)` |
| Sin sello, con claim, y con el emisor vivo o con D viva | `(false, null, rpc_unresolved)` |
| Sin sello, con claim, emisor muerto y D terminada (S4) | `(true, null, startup)` |
| `not-sent` (S2, N2) | `(true, false, <reason del sello>)` |
| R+ ∧ P ∧ Q ∧ Ex | `(true, true, null)` |
| `closed` (S3) con D viva / terminada | `(true, null, evidence_missing)` / `(true, null, startup)` |
| R− (`failure` de una sola id, E8c) | `(true, false, stale_state)` |
| `success` con perfil `coordinated` | `(true, null, unsupported)` |
| `success` sin Q válida, o `unrecognized` | `(true, null, evidence_missing)` |

R+ es `replied`/`success`. Q vale sólo si se leyó después de **este** sello
(nombra su `receipt`), en la misma vida D, y su identidad cumple la arista de
la fase; un hold exige además Stashed sin programar. En una retirada, otra
creación con el mismo id después de R+ cuenta como Absent de X. Absent o un
id reutilizado **sin respuesta** nunca atribuyen. Ex exige el perfil
`filtered`. `effect:false` con `stale_state` es un rechazo (R−); con otro
motivo es N2: el hold inicial cierra y las demás fases difieren. CC3 no añade
motivos al cable: todos están en `REASONS`.

Las filas de S2, R− y S3 no dependen de Q: con `post` null dan el mismo
veredicto. Un `success` sin Q (`post` null, porque la lectura venció o falló)
es provisional: la fase sigue `unknown`, no fija `hold_identity` y la reserva
queda pendiente de una Q posterior. Una Q incompatible también retiene. El
siguiente ciclo de CC2 que alcance la reserva vuelve a leer Q, sin reenviar
nada, con la garantía N/N+1 de la entrega CC2.

**Identidad que cotejan collect y apply.** Con el veredicto `(true, true,
null)`, P1 coteja la identidad de fase E = `rpc_effect_identity(op, P, Q)`,
nunca la Q cruda. E es Q, salvo en una retirada cuya Q muestra otra creación
con el mismo id en la misma generación. Entonces E es Absent de X: conserva
generación, id, creación, huella e intento de X, sin tiempos de intento.

- **Collect** compara `same_attempt(reserva.identity, E)`. El grupo que coteja
  es el de la reserva, porque una ausencia no tiene grupo y el de la tarea
  nueva no es el de X. `finish.expected` se coteja contra E.
- **Apply** exige `transition(expected, E, op)`, responde `retired` con
  `identity` = E y guarda E en la reserva.
- **Cable.** `validate_response` acepta ese recibo con `@1` intacto: la fase
  `retired` pide Absent (`response_phase`) y la arista Stashed→Absent o
  Done→Absent liga los campos estables. La Q cruda sólo sirve de testigo a
  `rpc_verdict` y nunca entra en un recibo ni en la reserva.

### Retenciones que quedan

R2 (daemon caído o reiniciado con la petición en vuelo), R4 (`held` perdido al
reiniciar antes del start) y R6 (Absent o id reutilizado sin respuesta) dejan
una reserva abierta para siempre y cierran el reordenar de su grupo. Un
emisor muerto sin claim, sea por un claim perdido o por un emisor que murió
antes de publicarlo, deja además `uncertain` mientras viva D. Eso retiene
también las aperturas del grupo. Sólo ocurre si muere CC1 entre registrar
`gated` y abrir la compuerta, si muere el emisor entre leer `dispatch` y
publicar el claim, o si una restauración pierde el claim. Un relevo
supervisado del daemon termina D y lo convierte en S4, como en los demás
cortes sin sello: `uncertain:false`, motivo `evidence_missing` y la reserva
abierta. La intervención explícita y auditada de las reservas que quedan
abiertas es un ticket posterior, antes de CC8, y no forma parte de P0. Editar
el diario no es un procedimiento.

## Entrega CC3-P0

P0 es contrato interno, schemas y corpus; no cambia el cable, ningún
escritor ni `client_actions.py`. `client_contract.py` añade `RPC_PLAN`,
`RPC_CLAIM` y `RPC_SEAL` con sus validadores puros y `parse_rpc_*`,
`execution_link`, `rpc_prior`, `pueue_greeting`, `rpc_effect_identity` y
`rpc_verdict`. Sus reglas
usan `rpc_require`, distinto de `require`, para que la paridad regla↔corpus de
CC0 siga intacta; `decode` delega en el mismo decodificador estricto con
límite explícito. `SCHEMA`, `OPS`, `REASONS`, `validate_request`,
`validate_response`, `decode` y `MAX_BYTES` no cambian.

`corpus/client-rpc-stock-v1.json` (`pao-client-rpc-corpus@1`) fija vidas,
emisores, peticiones por fase y observaciones sintéticas. Sus negativos cruzan
un solo campo por espécimen; la tabla de veredictos cubre S1–S4, N2, R+ con y
sin Ex, R− y los negativos del alcance. `corpus/pueue-404-frames.json`
(`pao-pueue-frames@1`) guarda las tramas doradas de los cuatro verbos de una
sola id con sus respuestas Success y Failure, la trama 2 `0.31.1` y saludos
rechazados. El secreto es un marcador.

`tests/test_client_contract_rpc.py` valida ambos corpus, límites, la paridad
de `rpc_require` con el corpus, la tabla de veredictos y el cable sin cambios.
`tests/mutate_client_contract.py` añade la mutación de cada regla RPC y once
mutantes semánticos, cada uno muerto por su aserción exacta: R+ sin Ex, sello
de un no dueño, S4 sin fin de la vida del daemon, `closed` tratado como
`replied`, Q tras un plazo vencido, id reutilizado tomado como Absent sin
respuesta, versión distinta en el saludo y, por la propuesta adoptada,
`closed` con el par vivo tomado como no aplicado. Los tres de la review r1 son
un claim perdido que liquida, una retirada que coteja la Q cruda y un sello
negativo de un no dueño.

Propuestas de E-stock:

- **S3 sin exigir el fin de la vida `peer`: adoptada.** E6 prueba que un
  cierre del daemon implica el fin de la tarea de la conexión. Sólo liquida:
  no atribuye ni libera la reserva. Con D viva el motivo es
  `evidence_missing`, porque no hubo reinicio. El congelamiento dentro del
  handler sigue sin corrida propia; lo cubre la fuente.
- **`remove` centinela como quinta forma cerrada: rechazada.** Sería una RPC
  mutadora sin plan, claim, sello ni atribución, y la entrega dejaría de ser
  una sola trama 3. Su inocuidad depende de que el id nunca se asigne, una
  afirmación negativa. Vuelca a disco cambios pendientes de terceros, no cubre
  cortes de energía porque el guardado no hace fsync, y R4 sigue necesitando
  el ticket posterior. El apagado ordenado ya guarda el Stashed (E9). El
  corpus de tramas fija exactamente las cuatro formas.

Correcciones de la review r1, sin cambiar el cable:

- **Claim perdido.** Sin claim y con el emisor muerto, `rpc_verdict` da
  `(false, null, evidence_missing)` mientras vive D y liquida por S4 con
  `(true, null, evidence_missing)` cuando D termina. Lo fijan las historias
  `lost-claim-*` y `claim-pending-*` y los mutantes «claim perdido que
  liquida» y «claim perdido como S4».
- **Retirada con id reutilizado.** `rpc_effect_identity` proyecta Absent de X
  para collect y apply. `rpc_attributed` usa la misma proyección. Lo fijan
  `test_retire_reused_id_projects_absent_of_x`, la historia
  `retire-interrupted-reused-id` y el mutante «retirada con Q cruda».
- **Turno de CC1.** El segundo acota sólo la espera del sello. P y Q llevan su
  propio plazo de lectura, y el texto ya no promete una cota del candado que
  las escrituras no permiten.
- **Nits.** El texto de CC2 distingue main de P1. Un emisor que aborta tras la
  trama 3 no sella `closed`. La historia `hold-not-sent-non-owner` y su
  mutante fijan el rechazo de un sello negativo de quien no es dueño.

Correcciones de la review r2, sin cambiar el cable:

- **Q vencida.** Q sólo se exige para atribuir un `success`. Lo fijan las
  historias con `post` null `hold-not-sent-startup`, `hold-failure-without-q`,
  `hold-closed-alive-without-q`, `hold-closed-daemon-gone` y
  `hold-success-without-q`, el invariante que exige una historia sin Q por
  clase de sello y el mutante «sin Q retiene todo».
- **Relevo de CC2.** El texto se ciñe a la garantía de la entrega CC2: lotes
  de ocho, 0,4 s compartidos, retención al vencer y N/N+1 sólo con el turno de
  la guardia.

El defecto latente de CC1 (confirmar `held` con rc 0 más la arista) queda
corregido en el contrato y en `rpc_verdict`; su cambio en `client_actions.py`
corresponde a P1. Las pruebas dirigidas no ejecutan daemon, SSH ni
`control.py`:

```text
python -I -m unittest discover -s fleet/pueue-desk/tests -p "test_client_contract*.py" -v
python -I fleet/pueue-desk/tests/mutate_client_contract.py
```

## Entrega CC3-P1

P1 añade el emisor stock y lo conecta a CC1 y CC2 con el perfil
`coordinated`. No cambia el cable, `client_contract.py`, los corpus ni Pueue
4.0.4. Ningún instalador de esta entrega fija `filtered`: `remote-run` sigue
inerte y los adaptadores, en `unsupported` hasta P2.

- **Emisor.** `client_rpc.py` usa sólo la biblioteca estándar. Lee `dispatch`
  de stdin y deriva de él cada ruta bajo `A/client-rpc/<dispatch>/`. Publica el
  claim con `link` exclusivo antes de conectar; el perdedor sale sin conectar
  ni sellar. Coteja el par del socket con la vida D del plan antes de la
  trama 1. Exige la trama 2 `0.31.1` exacta y, con cualquier otra, sella
  `not-sent`/`unsupported` sin escribir la trama 3. Sólo codifica las cuatro
  formas cerradas de una sola id, con el subconjunto CBOR. No reintenta, no
  reconecta y no toma candados. Un aborto local tras la trama 3 no sella.
  Ignora TERM, HUP, INT y PIPE.
- **CC1.** `deliver` sustituye a `send`. Bajo `control.lock`, lee la vida D de
  P antes de la reserva y crea el emisor en otra sesión, esperando en stdin.
  Después lee su nacimiento, publica el plan y registra
  `process={pid, birth, dispatch, plan_digest, state:'gated'}`. Sólo entonces
  escribe `dispatch` y pasa a `sent`. Espera el sello como máximo
  `SEAL_SECONDS` (1 s) y nunca mata ni recoge al emisor. Cada lectura del
  daemon del turno lleva `READ_SECONDS` (0,2 s) en `Pueue.timeout` y
  `Pueue.deadline`. Q se lee sólo tras un sello `success` y nombra su
  `receipt`. `settle` aplica `rpc_verdict`. El efecto atribuido guarda
  `attribution={dispatch, seal_receipt, exclusion}`. R− y N2 cierran el hold
  inicial y difieren las demás fases. S3, S4 y un `success` sin Q quedan
  `unknown`, con `uncertain:false` y la reserva abierta. El rc del CLI y la
  arista ya no confirman nada: queda corregido el defecto latente.
- **CC2.** Una fase del emisor en `unknown` se recoge con `collect_stock`,
  fuera del candado y en el proceso del lote. Lee vidas y originales; lee una
  Q nueva sólo tras un sello `success`. apply recalcula `rpc_verdict` bajo el
  candado con originales y vidas frescos, sin leer el daemon, y con la Q de la
  propuesta. Un sello válido se recoge aunque el emisor siga vivo. Un
  `success` sin Q o con una Q incompatible sigue reintentable en cada ciclo
  que alcance la reserva (N/N+1). Si vence el plazo del lote, retiene con
  `evidence_missing`. Los hijos sin plan conservan la regla anterior.
- **Perfil.** `control.CLIENT_EXCLUSION = 'coordinated'`. Con él, las fases
  mutadoras de `remote-run` responden `unsupported` antes de crear un plan o
  un emisor. Sólo `filtered` habilita la atribución, y lo fijará el
  instalador de CC7.

`tests/fake_pueued.py` es un daemon sintético que sólo habla tramas sobre un
socket Unix. `tests/test_client_rpc.py` ejecuta el emisor real con
nacimientos nativos. Recorre cada frontera de §2.6 con D viva y con D
terminada:

| Caso | Resultado |
|---|---|
| Antes del claim | Retiene; liquida por S4 con `evidence_missing` |
| Entre el claim y la trama 3 | `rpc_unresolved`; liquida por S4 con `startup` |
| Después de la trama 3 | `rpc_unresolved`; liquida por S4 con `startup` |
| Respuesta perdida | S3, nunca se atribuye |

También cubre el claim perdido y el perdedor del claim, la Q vencida y el
relevo de CC2, otra versión en el saludo, otra vida en el par y el perfil
`coordinated`. Para el defecto latente prueba un `success` con Q en
`Running`. Las tramas doradas de `corpus/pueue-404-frames.json` fijan el
codificador. `tests/mutate_client_rpc.py` mata doce mutantes por aserción.
`mutate_client_actions.py` reapunta dos casos a la compuerta y a la falla
tras la intención.

```text
python -I -m unittest discover -s fleet/pueue-desk/tests -p "test_*.py" -v
(cd fleet/pueue-desk/tests && python -B mutate_client_rpc.py)
```
