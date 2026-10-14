# Fuentes: propuesta OpenCode Go

> Archivo histórico de la selección inicial. La selección vigente sustituye
> K2.7 Code por Kimi K3. Consulta el [selector actual](../../docs/agent-routing.md)
> y la [guía actual](../../docs/opencode-workers.md).

Consulta pública: 2026-09-10. Investigación sin inferencia, credenciales, instalación ni configuración.

## Selección recomendada, todavía por validar con tareas propias

**DeepSeek V4.1 Flash + MiniMax M3 + Kimi K2.7 Code.** Son una propuesta operativa; no una clasificación empírica de inteligencia o gusto.

- **DeepSeek V4.1 Flash:** trabajador económico principal solicitado; versión DeepSeek más nueva documentada en Go y beneficiaria de la promoción temporal 4× de su portada. [Go](https://opencode.ai/docs/go/), [promoción](https://opencode.ai/go).
- **MiniMax M3:** complemento para implementación frontend/backend y revisión visual contra un diseño aprobado. Su fabricante declara entrada nativa de imágenes y vídeo, contexto de 1M y avances en programación; publica 59.0% SWE-Bench Pro y 66.0% Terminal-Bench 2.1. Son resultados del fabricante bajo sus harnesses; no se ejecutaron aquí ni prueban mejor gusto estético. [Anuncio M3](https://www.minimax.io/blog/minimax-m3).
- **Kimi K2.7 Code:** complemento de programación prolongada y seguimiento de instrucciones. Moonshot declara aproximadamente 30% menos tokens de razonamiento que K2.6 y mejores resultados coding/agent. Es preferible para este presupuesto a reservar uno de sólo tres puestos para K3. [Anuncio K2.7 Code](https://www.kimi.com/resources/kimi-k2-7-code).

K3 sigue siendo candidato si prima calidad visual sobre cuota: Moonshot lo presenta como su flagship más capaz y destaca diseño visual frontend, razonamiento y programación prolongada. Esa afirmación comercial no demuestra que K2.7 o M3 tengan peor gusto en nuestros proyectos. [Kimi Code oficial](https://www.kimi.com/code/docs/en/).

GLM-5.3 es otra alternativa de capacidad: Z.ai destaca contexto 1M y tareas prolongadas. GLM-5.3-Flash sirve como alternativa económica multimodal; falta comparación propia contra M3 para justificar sustituirlo. [ZCode](https://zcode.z.ai/en/docs/welcome), [modelos GLM](https://autoclaw.z.ai/models/).

## Datos comerciales publicados

USD por millón de tokens; cuota mensual equivalente usando sólo ese modelo. «—» significa sin tarifa publicada para escritura de caché. [Tabla Go oficial](https://opencode.ai/docs/go/).

| Modelo | ID Go (añadir `opencode-go/`) | Entrada | Salida | Caché lectura | Caché escritura | Cuota base |
|---|---|---:|---:|---:|---:|---:|
| DeepSeek V4.1 Flash, valle/pico | `deepseek-v4.1-flash`* | 0.15 / 0.30 | 0.60 / 1.20 | 0.003 / 0.006 | — | 15 |
| MiniMax M3 | `minimax-m3` | 0.30 | 1.20 | 0.06 | — | 60 |
| Kimi K2.7 Code | `kimi-k2.7-code` | 0.95 | 4.00 | 0.19 | — | 60 |

Pico DeepSeek: lunes–viernes 01:00–04:00 y 06:00–10:00 UTC; resto valle. Go cuesta 10 USD/mes; las ventanas son 20%/5h, 50%/semana y 100%/mes. La opción «Use balance» permite consumir saldo Zen al agotar Go. Los límites pueden cambiar. [Documentación](https://opencode.ai/docs/go/).

La portada confirma **4× temporal para V4.1 Flash**, 26,000 solicitudes estimadas/5h y 60 USD de cuota equivalente frente a base 6,500 y 15 USD. No publica vencimiento exacto; **no prometer tres días ni beneficio permanente**. K2.7 anuncia 1,350 solicitudes/5h, M3 3,200, K3 110. Estas estimaciones usan mezclas de tokens distintas y no equivalen a tareas terminadas. [Portada oficial](https://opencode.ai/go).

## Cuota compartida e IDs publicados

El código público aplica `quotaCost = cost × modelInfo.costMultiplier` a los mismos contadores mensuales, semanales y de cinco horas de `LiteTable`: tres perfiles no crean tres bolsas independientes. Es evidencia de implementación pública, no inspección del despliegue ni saldo de esta cuenta. [handler.ts, líneas 1184–1232, commit 43e89ea](https://github.com/anomalyco/opencode/blob/43e89ea1673937c325edb922eaa1a768aa0e5bf4/packages/console/app/src/routes/zen/util/handler.ts#L1184).

* **Ambos IDs DeepSeek están publicados:** documentación Go enumera `deepseek-v4.1-flash`; código oficial de la portada usa `deepseek-flash` para V4.1. [go-models.ts, líneas 25–35, mismo commit](https://github.com/anomalyco/opencode/blob/43e89ea1673937c325edb922eaa1a768aa0e5bf4/packages/console/app/src/component/go-models.ts#L25). El orquestador verificó independientemente mediante lectura web del [catálogo público JSON](https://opencode.ai/zen/go/v1/models) la presencia de ambos IDs, además de `minimax-m3` y `kimi-k2.7-code`. El HTTP 403 obtenido desde Python fue específico de ese cliente y no un bloqueo global del catálogo. Recomendar `opencode-go/deepseek-v4.1-flash` para seleccionar explícitamente la versión publicada; esto no garantiza inmutabilidad futura del backend. Sigue pendiente comprobar `opencode models` local antes de activar: el CLI no está disponible en PATH.

## Límites de esta evaluación

No hay pruebas comparativas locales de inteligencia, gusto, latencia, tasa de éxito ni coste por tarea aceptada. No se confirma exposición de imagen/vídeo o 1M contexto a través de Go sólo porque el modelo directo lo ofrezca. Kimi Code directo exige Thinking para servir K2.7/K3 y de lo contrario enruta K2.6; no se ha demostrado la misma regla en Go. [Configuración Kimi](https://www.kimi.com/code/docs/en/kimi-code/models.html).

La selección prioriza coste por capacidad utilizable y diversidad de tareas. Si el objetivo fuese exclusivamente la mayor capacidad publicada, K3/GLM-5.3 merecen otro ensayo; para cheap coders, su cuota de 15 y sus tarifas reducen mucho el margen de iteración. No se ha otorgado autorización ni acreditación a ninguno.
