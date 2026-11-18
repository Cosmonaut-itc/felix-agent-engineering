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

Son tres acreditaciones distintas:

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
waiter ni HTTP. La guardia lo ejecuta antes de recuperar/admitir, incluso si
el relevo mantiene el tick cerrado. `prepare_start` lo ejecuta antes de leer
su imagen de preparación; el ensayo no escribe ni recoge. El preparador
conserva el Stashed cliente, la incertidumbre y las reservas a través del
reinicio. No convierte una reserva cliente en propiedad del editor de orden.

El ciclo toma una copia de acción/reserva bajo el mismo inode de control,
suelta el candado, recoge del daemon y del adaptador fijo, y recupera el
candado. Antes de guardar coteja la generación durable y la observada, la
revisión y digest de la fase, y revisión/digest/fase/apertura de la reserva.
Una copia obsoleta se descarta; el índice la vuelve a leer en otro tick.
No espera result.lock ni llama al lanzador bajo control.lock. Los ámbitos
productivos usan `locked`, que conserva el descriptor mientras permite esa
ventana; después se relee la autoridad. El wrapper de arranque vuelve a
comprobar la autorización del relevo antes de ejecutar el daemon.
La recuperación del descriptor exterior usa un plazo de 0,1 s: si otro dueño
lo retiene, lanza `ClientCollectionBusy` con `client_collection_lock_busy` y
aborta el tick/preparador; ningún trabajo que presuponga exclusión continúa.
El siguiente tick puede reintentar. Este plazo y los 0,4 s de recogida no son
una cota de extremo a extremo: copia, cotejos, SQLite y lecturas de generación
son trabajo adicional.

Se recogen hasta ocho reservas por lote. Un único proceso de recogida tiene
un plazo de 0,4 s para todo el lote; el RPC de lectura tiene un plazo menor.
Se recoge el proceso y su grupo privado al salir. Una lectura lenta o sin
prueba retiene con `evidence_missing`, sin impedir el cierre por presión de
la guardia. No hay espera de gracia, llamada recursiva a client ni RPC
mutadora desde el reconciliador.

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

El futuro método fijo `recover` recibe copia de la petición, estado, reserva,
proceso identificado y observación del daemon. Devuelve observación acreditada,
exclusión positiva de RPC tardía (`settled`), prueba del efecto de **esta fase**
(`effect_applied:true`) y opcionalmente una petición finish autenticada por
`verify`. Son pruebas distintas: `settled` no puede derivarse sólo del estado
del hijo ni del daemon, y tampoco prueba quién produjo el estado observado.
Tras un gated incierto, incluso un Stashed con la identidad esperada más
settled conserva `unknown`, reserva abierta/incierta y `hold_identity:null`
si falta la prueba de efecto. Un adaptador antiguo que omite effect_applied
retiene igual. cancel-no-payload y retire-held rechazan sin RPC hasta acreditar
la atribución; no basta excluir mensajes tardíos. CC3–CC5 deben acreditar
ambas pruebas por lanzador.
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
