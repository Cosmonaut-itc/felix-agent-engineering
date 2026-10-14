# Validación del esfuerzo de Kimi K3 en OpenCode Go

Fecha: 2026-09-10. CLI: OpenCode 1.18.30. Proveedor: `opencode-go`.

## Dictamen

- **PASS:** configuración de variantes, serialización de `reasoning_effort`, envío por el transporte nativo y aceptación de `low`/`high` por Go.
- **PASS:** ambas sesiones usaron `cheap-coder-kimi`, proveedor Go y modelo `kimi-k3`; devolvieron el resultado correcto, sin herramientas ni modificaciones.
- **NO OBSERVABLE:** nivel aplicado dentro del proveedor. El detector no encontró claves ni cabeceras con `effort` en su nombre, y no identificamos una lectura explícita del esfuerzo en los metadatos observados. El resultado no demuestra que Go ignore ni que aplique el parámetro.
- El perfil habitual conserva `disable: true`, conforme al criterio actual de activación. Las pruebas habilitaron únicamente copias temporales mediante overrides de proceso.

## Configuración efectiva

El perfil pide `reasoningEffort: low` y conserva `thinking: {type: enabled}`.
El catálogo resuelto contiene sólo variantes `low` y `high`; `max` y `xhigh` están deshabilitadas en la configuración. Esto valida las variantes configuradas, no constituye un sandbox contra overrides posteriores.

Evidencia: [effective-config.json](effective-config.json).

## Ejecución directa observada

Mismo prompt de usuario: hallar el menor positivo con residuos 2 módulo 5, 3 módulo 7 y 4 módulo 9. Resultado independiente: 157. Una respuesta por variante, presupuesto externo de 120 segundos y un paso por sesión.

| Variante CLI | Valor enviado | HTTP | Duración llamada observada | Tokens de razonamiento reportados | Resultado |
|---|---|---:|---:|---:|---|
| `low` | `low` | 200 | 4.669 s | 423 | 157, correcto |
| `high` | `high` | 200 | 13.331 s | 545 | 157, correcto |

Las duraciones y tokens son observaciones de una sola ejecución por variante, no un benchmark ni prueba causal del esfuerzo aplicado. Los contextos de sesión tienen pequeñas diferencias; el prompt de usuario es igual.

- [Captura low](native-low-wire.jsonl) y [captura high](native-high-wire.jsonl): modelo, parámetros, HTTP, nombres de cabeceras, IDs de respuesta y uso.
- [Sesión low](native-low-export.json) y [sesión high](native-high-export.json): lectura independiente de agente/proveedor/modelo/variante y respuesta.
- [Resumen de procesos](native-summary.json): exit code 0 para ambos.

## Instrumentación y alcance

[capture.js](capture.js) envuelve `globalThis.fetch` dentro de un plugin temporal de OpenCode. Invoca la función original con los mismos argumentos, sin cambiar URL, cuerpo ni cabeceras. Lee una copia de la respuesta y guarda únicamente una lista explícita de metadatos; no guarda cabeceras de autorización ni el cuerpo del prompt. La lectura de la copia puede influir en el tiempo observado por el consumidor; la latencia no se usa como acreditación.

[native.py](native.py) carga el plugin sólo en un directorio temporal, desactiva MCP en esos procesos, habilita temporalmente Kimi y fija modelo/variante explícitos. El plugin temporal fue retirado y los procesos terminaron.

El primer intento mediante un relé Python recibió cuatro respuestas HTTP 403 de Cloudflare (incluyendo peticiones auxiliares); no acredita aceptación. Se conserva separado en `wire-metadata.json`, `summary.json` y `validate.py`. Las dos pruebas aceptadas usaron exclusivamente el transporte nativo sin relé ni cambios de identidad o cabeceras.

## Contrato público y límite

[Kimi API](https://platform.kimi.ai/docs/guide/use-reasoning-effort) documenta low/high/max para K3 y recomienda retirar la opción thinking heredada de K2.x. [Kimi Code](https://www.kimi.com/code/docs/en/kimi-code/models.html) tiene un contrato y default distintos. En estas pruebas Go aceptó el perfil instalado, incluido thinking enabled; no se cambió su configuración basándose en contratos de otro endpoint.

[OpenCode Models](https://opencode.ai/docs/models/#custom-variants) documenta variantes configurables. [Go](https://opencode.ai/docs/go/) no aporta en la documentación examinada una lectura del esfuerzo aplicado para cada respuesta.

La revisión de código público fijó el commit `193de13a88d62a6409c6d385831180f1def527dc`: [requestBody.ts](https://github.com/anomalyco/opencode/blob/193de13a88d62a6409c6d385831180f1def527dc/packages/console/app/src/routes/zen/util/requestBody.ts) preserva ese campo en la ruta legada; [inference-proxy.ts](https://github.com/anomalyco/opencode/blob/193de13a88d62a6409c6d385831180f1def527dc/packages/console/app/src/lib/inference-proxy.ts) muestra otra ruta para cuentas migradas. Esto no identifica la ruta interna usada por esta cuenta ni prueba su semántica final.

Para cerrar el criterio estricto restante haría falta metadata explícita por petición, trazabilidad upstream verificable o confirmación oficial aplicable al endpoint Go. Repetir tareas y comparar tokens no reemplaza esa evidencia.

## Revisión independiente

Un revisor gpt-6-astra en low aceptó el dictamen tras examinar la instrumentación, las capturas y exportaciones. El contador diagnóstico `reasoning_chars` sólo reconoce `delta.reasoning_content` y devolvió 0; no mide todas las representaciones de razonamiento. Las partes de razonamiento de las sesiones y los tokens reportados sí están presentes. Ese contador no se utiliza en el dictamen.
