# Borrador: despachar trabajadores mediante OpenCode Go

> Archivo histórico de la selección inicial. La selección vigente sustituye
> K2.7 Code por Kimi K3. Consulta el [selector actual](../../docs/agent-routing.md)
> y la [guía actual](../../docs/opencode-workers.md).

Estado: propuesta, no instalada ni probada con inferencia. 2026-09-10.
Destino propuesto: `docs/opencode-workers.md` → `~/.agents/docs/opencode-workers.md`.

Lee esta guía antes de despachar desde Codex o Claude Code a un cheap coder
de Go. La selección pertenece a `agent-routing.md`; esta guía define el
transporte. El orquestador conserva la aceptación del resultado.

## Ruta elegida

Usar **OpenCode CLI como trabajador externo**. OpenCode proporciona el bucle
de herramientas, archivos y sesiones. Una petición directa al endpoint del
modelo sólo genera una respuesta; integrar ese endpoint exigiría construir
también ese bucle. Para muchos encargos, la alternativa es el servidor HTTP
de OpenCode. El SDK oficial envuelve esa API.

La ruta CLI evita un servicio persistente para despachos individuales; HTTP
facilita identificar una sesión antes de enviar el encargo y cancelarla por
ID. Comenzar con CLI y conservar la alternativa HTTP documentada.

## Preparar

1. Comprobar ejecutable y versión con `command -v opencode` y
   `opencode --version`. Verificar las opciones de la versión instalada con
   `opencode run --help`. Instalar/configurar sólo dentro del alcance aprobado.
2. Conectar el proveedor Go por su flujo de autenticación y comprobar
   `opencode models opencode-go --verbose`. Registrar IDs y variantes; nunca
   incluir credenciales en prompts, repositorio o evidencia.
3. Validar los perfiles con `opencode agent list`, la configuración efectiva
   y el catálogo. El modelo y perfil solicitados deben existir antes de lanzar.
4. Asignar directorio real, archivos permitidos y presupuesto. Dar al
   trabajador un encargo autocontenido con contexto, criterios de aceptación,
   comprobaciones y formato de resultado. No hereda el chat de origen.

Comandos de catálogo, ejecución, reanudación y exportación se documentan en
la [referencia CLI](https://opencode.ai/docs/cli/).

| Perfil propuesto | Modelo explícito |
|---|---|
| `cheap-coder-deepseek` | `opencode-go/deepseek-v4.1-flash` |
| `cheap-coder-minimax` | `opencode-go/minimax-m3` |
| `cheap-coder-kimi` | `opencode-go/kimi-k2.7-code` |

El [catálogo público](https://opencode.ai/zen/go/v1/models) consultado publica
los tres IDs y también `deepseek-flash`. Preferir el ID versionado de DeepSeek;
confirmar su resolución efectiva antes de usarlo. Publicado no significa
autenticado ni probado en esta cuenta.

## Perfil y controles efectivos

Los perfiles usarán `mode: all`: pueden actuar como agentes directos del
runtime OpenCode y como subagentes cuando corresponda. Son trabajadores
delegados desde el punto de vista de Codex/Claude Code. **`mode: subagent`
no sirve para el lanzamiento directo con `opencode run --agent`**: el código
examinado avisa y recurre al agente por defecto. También lo hace si el nombre
no existe. Verificar antes del despacho y tratar cualquier sustitución como
fallo de transporte. [Código CLI](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/run.ts).

Configurar permisos concretos para el encargo y denegar `task` si no se
autorizaron descendientes. En un encargo de lectura, denegar edición y shell
de escritura; en uno de implementación, autorizar sólo superficie y comandos
necesarios. El directorio de trabajo y un worktree no constituyen un sandbox.
El alcance también debe cubrir procesos lanzados desde shell y MCP heredados.

Propuesta inicial: `steps: 20` y plazo externo de 10 minutos por despacho,
ajustables al preparar el encargo. `steps` limita iteraciones, no tokens ni
dólares. Un agotamiento devuelve trabajo parcial para veto, no éxito.
[Agentes y permisos](https://opencode.ai/docs/agents/).

La configuración se combina con otras capas. Los overrides de ejecución
`OPENCODE_CONFIG_CONTENT` tienen precedencia; `OPENCODE_CONFIG` por sí solo
puede ser sobrescrito por configuración del proyecto. Inspeccionar el
resultado efectivo y mantener los cambios del despacho fuera de la
configuración global. [Precedencia](https://opencode.ai/docs/config/).

| Modelo | Control propuesto y evidencia |
|---|---|
| DeepSeek V4.1 Flash | `--variant low` de inicio; `high` si hace falta. Verificar que se transmite `reasoning_effort`; DeepSeek mapea medium a high. |
| MiniMax M3 | `--variant thinking` para coding; `none` sólo para encargos apropiados. OpenCode traduce thinking a modo adaptativo, no a un nivel high. |
| Kimi K2.7 Code | En la ruta Go compatible con OpenAI, el código examinado no genera variantes Kimi. Fijar y verificar el modo nativo soportado en el perfil/proveedor; no inventar `--variant low`. |

Fuentes: [DeepSeek](https://api-docs.deepseek.com/guides/thinking_mode/),
[transformaciones de OpenCode](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts).
La excepción de modos nativos debe aprobarse en el routing antes de activar
MiniMax/Kimi. El mapeo de Kimi a través de Go queda por validar en la prueba de
activación; la API directa de Kimi no acredita el comportamiento del proxy Go.

## Despachar por CLI

Ejemplo documental, con perfil ya validado, rutas sustituidas por las reales
y directorio de evidencia existente:

```sh
opencode run \
  --dir /ruta/absoluta/worktree \
  --agent cheap-coder-deepseek \
  --model opencode-go/deepseek-v4.1-flash \
  --variant low \
  --format json \
  < /ruta/absoluta/evidencia/encargo.md \
  > /ruta/absoluta/evidencia/eventos.jsonl \
  2> /ruta/absoluta/evidencia/stderr.log
```

El lanzador programático debe ejecutar el binario con un **array de argumentos**,
`cwd` explícito y el archivo de encargo por stdin. Conservar PID o ID del
proceso del harness y su código de salida. No interpolar el prompt como código
de shell. OpenCode acepta stdin; JSON emite registros con `sessionID`.
Los eventos observados en el código incluyen `text`, `tool_use`, `step_finish`
y `error`; no asumir un evento ficticio de tarea aceptada.
[Implementación de run](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/run.ts).

En ejecución no interactiva, los permisos pendientes pueden ser rechazados.
Preparar los permisos autorizados antes de lanzar; un rechazo se conserva
como evidencia. No resolverlo habilitando aprobación indiscriminada.

## Recoger, continuar y detener

- Seguir el proceso hasta terminación. Conservar stdout, stderr, código de
  salida y `sessionID`, incluyendo errores y comprobaciones fallidas.
- Exportar con `opencode export <sessionID>` y verificar en los mensajes el
  proveedor/modelo/agente usado. Conservar la configuración efectiva redactada
  para acreditar controles solicitados; si no hay lectura del modo realmente
  aplicado, reportar esa limitación.
- Reanudar el mismo encargo con `opencode run --session <sessionID>`, repitiendo
  directorio, perfil, modelo, controles y `--format json`; enviar la corrección
  por stdin y guardar nueva evidencia. Evitar `--continue`, que selecciona una
  sesión por recencia.
- Para ejecución CLI independiente, el lanzador debe disponer de un grupo de
  procesos propio: al cancelar, detenerlo y verificar la salida y los procesos
  hijos. Si se usa servidor, cancelar la sesión por la API antes de cerrar el
  cliente; matar un cliente conectado no demuestra que el servidor se detuvo.
- Revisar diff completo y criterios. Exit code 0, texto final o fin de un paso
  no acreditan aceptación. El orquestador decide aceptar, corregir o rechazar;
  conserva evidencia antes de retirar recursos.

## Alternativa HTTP para despacho asíncrono

Ejecutar `opencode serve --hostname 127.0.0.1 --port <puerto>` desde el
directorio asignado; identificar el proceso y protegerlo con autenticación.
Consultar `/doc` de esa versión para validar el esquema, incluida la variante.

| Operación | Petición |
|---|---|
| Crear e identificar encargo | `POST /session` con título; guardar `id`. |
| Despachar | `POST /session/:id/prompt_async` con agente, modelo y partes. |
| Recoger | `GET /session/status`, `GET /session/:id/message` y eventos SSE. |
| Continuar | Otro prompt a la misma sesión, con selección explícita. |
| Cancelar | `POST /session/:id/abort`; comprobar estado y procesos iniciados. |

El cuerpo del mensaje identifica el modelo así:

```json
{
  "agent": "cheap-coder-deepseek",
  "model": {
    "providerID": "opencode-go",
    "modelID": "deepseek-v4.1-flash"
  },
  "variant": "low",
  "parts": [{ "type": "text", "text": "Encargo autocontenido aprobado" }]
}
```

Un HTTP 204 sólo acredita envío. La recogida debe distinguir respuesta final,
estado idle, errores y aceptación. Usar un servidor por directorio cuando se
busque separación sencilla; no mutar `/config` de un servidor compartido para
cambiar el modelo entre encargos. [API](https://opencode.ai/docs/server/),
[SDK](https://opencode.ai/docs/sdk/).

## Estado de validación

Contrato, documentación pública, catálogo público y código fuente examinados.
No se ha ejecutado un trabajador OpenCode. Antes de habilitar esta ruta:
validar versión/perfiles/opciones, un encargo por modelo, read-back del modelo,
reanudación, cancelación y fallo ante perfil inválido. Los tres perfiles y un
lanzador con estos controles se crearían en la fase aprobada de implementación.
