# Delegar por T3 Code

Mecanismo común de las rutas cruzadas cuando el orquestador corre en un hilo
de T3 Code: Claude Code → GPT y Codex → Claude. Cada guía de ruta fija la
instancia, los modelos, las opciones, sus condiciones propias y el respaldo.
Este documento fija el resto.

`delegate_task`, del MCP `t3-code`, lanza un hijo que pertenece al hilo de
T3. T3 lo sigue, despierta al hilo cuando termina y lo detiene.

## Elegir la ruta

T3 es la primera opción cuando se cumplen las tres condiciones:

- Existe la herramienta `delegate_task`. Su nombre depende del harness:
  `mcp__t3-code__delegate_task` en Claude Code y
  `mcp__t3_code__delegate_task` en Codex. El servidor puede conectarse
  tarde: antes de darla por ausente, en Claude Code búscala una vez con
  ToolSearch y en Codex intenta una llamada directa a
  `orchestrator_capabilities`.
- `orchestrator_capabilities` lista la instancia de destino con
  `canRunChildTask: true` y el modelo que pide el routing.
- Se cumplen las condiciones propias de la guía de ruta.

Usa el respaldo de la guía de ruta si T3 no está disponible, rechaza el
lanzamiento o el encargo necesita tope duro de tiempo. Al despachar, di en
una línea qué ruta usaste y, si caíste al respaldo, por qué.

Si la llamada a `delegate_task` falla sin respuesta clara, T3 pudo aceptar el
encargo. Repítela con el mismo `clientRequestId`. Si el encargo existe,
síguelo, o cancélalo y confirma que se detuvo antes de lanzar el respaldo.

## Despachar

```json
{
  "task": "<brief completo>",
  "title": "<encargo corto>",
  "role": "implementation",
  "target": {
    "providerInstanceId": "<instancia de la guía de ruta>",
    "model": "<id completo>",
    "options": { "<opción de esfuerzo>": "<nivel>" }
  },
  "mode": "async",
  "isolation": "worktree",
  "clientRequestId": "<slug>-<ronda>"
}
```

- `model` lleva el id completo que lista `orchestrator_capabilities`. El
  esfuerzo sale del routing, con tope `high` aunque T3 ofrezca niveles
  mayores. Pasa `options` siempre: sin ellas el hijo toma el default del
  modelo. Comprueba `providerInstanceId`, `model` y `options` en la
  respuesta.
- Deja `runtimeMode` sin fijar. Un modo más estrecho no da sólo lectura: el
  hijo espera una aprobación que nadie da.
- T3 no tiene sandbox de sólo lectura. Un encargo read-only lo dice en el
  brief, con `isolation: "shared"`, y compruebas `git status` del checkout
  antes y después.
- Para escribir, usa `isolation: "worktree"`. El worktree parte del último
  commit local y no copia cambios sin commitear: commitea antes. Localiza el
  worktree del hijo con `t3_worktree_list` e integra tú sus cambios.
- `clientRequestId` cambia en cada ronda y se repite en los reintentos de la
  misma ronda.
- El `taskId` identifica el encargo. `childThreadId` es sólo el
  almacenamiento del hijo.

## Recoger

Cuando el hijo termina, una notificación despierta al hilo: termina tu turno
en vez de sondear. Usa `task_status` sólo si necesitas el resultado a mitad de
turno; `workState: result_available` y `summary` traen el resultado. El hijo
puede quedar esperando de dos formas:

- `hasPendingQuestion`: lista la pregunta con `t3_pending_request_list` y
  `threadId: childThreadId`, léela con `t3_pending_request_read` y su
  `requestId`, y contéstala con `t3_pending_request_respond` o pregunta al
  usuario.
- `pendingApproval`: esas herramientas no aprueban. Pide al usuario que
  decida en el hilo del hijo.

## Corregir y revisar

Cada ronda de corrección o de review es un `delegate_task` nuevo con el brief
original, los hallazgos previos, las respuestas y las objeciones abiertas.

## Detener

`task_cancel` con el `taskId` detiene al hijo y a sus descendientes. T3 no
impone tope de tiempo, y un hijo colgado no despierta al hilo. Los minutos del
brief orientan al trabajador, pero no lo detienen.

## Cerrar

Archiva el hilo de cada hijo que cierras, aceptado, rechazado o cancelado,
con `t3_thread_organize`, `action: "archive"` y `threadId: childThreadId`.
Cada ronda deja su propio hijo, así que archiva todos. Hazlo después de
integrar su worktree, porque archivar detiene su sesión y sus terminales. El
hilo archivado sigue legible con `t3_thread_read`, pero rechaza mensajes:
para retomarlo, desarchívalo con `action: "unarchive"`. Cada padre archiva
los hijos que recogió y no archiva un hilo con hijos vivos, porque T3 no
entrega resultados a un padre archivado.

Cada hilo sin archivar encarece las consultas de T3 que recorren la lista de
hilos, y un día de delegación deja cientos. Si el archivado falla, dilo al
cerrar.
