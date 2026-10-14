# Propuesta: tres cheap coders mediante OpenCode Go

> Archivo histórico de la selección inicial. La selección vigente sustituye
> K2.7 Code por Kimi K3. Consulta el [selector actual](../../docs/agent-routing.md)
> y la [guía actual](../../docs/opencode-workers.md).

Estado: pendiente de aprobación. Consulta: 10 de septiembre de 2026.
Estos archivos son borradores; no modifican el contrato instalado.

## Selección

Propongo **DeepSeek V4.1 Flash, MiniMax M3 y Kimi K2.7 Code** como la mejor
combinación inicial para ejecución económica. Es una recomendación de
capacidad por presupuesto, no un ranking absoluto de benchmarks.

Mantendría las puntuaciones actuales y añadiría estas filas:

| Modelo | costo | inteligencia | gusto | Cuándo |
|---|---:|---:|---:|---|
| DeepSeek V4.1 Flash · Go | 10* | 8 | 6 | Primera opción económica para encargos técnicos acotados y verificables. |
| MiniMax M3 · Go | 9 | 8 | 7 | Implementación de frontend/backend y trabajo visual contra un diseño aprobado. |
| Kimi K2.7 Code · Go | 8 | 8 | 7 | Cambios de varios archivos, contexto amplio y ejecución de planes de programación. |

Escala: 10 es mejor; en **costo significa más económico**. Inteligencia y
gusto son hipótesis operativas iniciales, todavía sin evaluación en tus
proyectos. El gusto valora criterio de producto, claridad y coherencia visual;
tener visión no demuestra buen gusto. Los empates de inteligencia son
intencionales: no hay evidencia propia para una precisión mayor.

*DeepSeek recibe costo 10 mientras la promoción 4× esté vigente. Propongo
costo 8 fuera de promoción, sujeto a las tarifas y cuota vigentes. El selector
debe verificar la promoción, no perpetuarla por haber leído este documento.

MiniMax documenta avances en frontend/backend y capacidades multimodales;
Moonshot presenta K2.7 Code como modelo para programación prolongada y reporta
menor consumo de razonamiento que K2.6. Son resultados de sus fabricantes,
no pruebas locales. [MiniMax](https://www.minimax.io/blog/minimax-m3),
[Moonshot](https://www.kimi.com/resources/kimi-k2-7-code).

Kimi K3 y GLM-5.3 merecen consideración cuando prime capacidad sobre cuota;
sus condiciones actuales dejan menos margen para iterar. No usaría esos dos
como primeras incorporaciones económicas. GLM-5.3-Flash sería un candidato
de sustitución tras comparar resultados reales. [Condiciones Go](https://opencode.ai/docs/go/).

## Coste monetario y cuota

Go cuesta **10 USD/mes por suscripción**, no por cada perfil. Tarifas publicadas
en USD por millón de tokens:

| Modelo | Entrada | Salida | Lectura de caché | Cuota mensual equivalente |
|---|---:|---:|---:|---:|
| DeepSeek V4.1 Flash, valle / pico | 0.15 / 0.30 | 0.60 / 1.20 | 0.003 / 0.006 | 15 base; 60 durante promoción |
| MiniMax M3 | 0.30 | 1.20 | 0.06 | 60 |
| Kimi K2.7 Code | 0.95 | 4.00 | 0.19 | 60 |

Las tarifas sirven para contabilizar uso incluido; no son tres mensualidades
ni bolsas sumables. Ventanas: 20% cada cinco horas, 50% semanal, 100% mensual.
[Tarifas](https://opencode.ai/docs/go/).

La portada confirma **4× temporal para DeepSeek V4.1 Flash** y muestra
26,000 solicitudes estimadas por cinco horas frente a 6,500 base. No publica
fecha exacta de vencimiento. Las solicitudes no equivalen a tareas terminadas.
[Promoción](https://opencode.ai/go).

El código oficial contabiliza los modelos sobre los mismos contadores de
Go, aplicando un multiplicador por modelo. Cambiar de perfil no recupera
cuota agotada. [Implementación de cuota](https://github.com/anomalyco/opencode/blob/43e89ea1673937c325edb922eaa1a768aa0e5bf4/packages/console/app/src/routes/zen/util/handler.ts#L1184).

## Cambios propuestos al selector y contrato

1. Añadir las tres filas a `docs/agent-routing.md` y definir el sentido de
   costo. Conservar la política de elegir el trabajador económico adecuado,
   coste total del encargo y prioridad inteligencia > gusto > costo.
2. Reemplazar las «dos monedas» por **tres presupuestos: Codex, Claude y Go**.
   Con cuota suficiente y capacidad adecuada, preferir Go para ejecución
   acotada. Elegir entre los tres por tipo de trabajo, sin imponer una cadena
   de reintentos por todos. Escalar por evidencia de insuficiencia.
3. Precisar que lo deprecado es el MCP antiguo `stealth/ox-alpha`. Los nuevos
   cheap coders se ejecutan por OpenCode y conservan el ciclo
   Despachar → Recoger → Vetar → Cerrar.
4. Mantener el primer pase independiente Astra y el pase Opus para alto
   riesgo. MiniMax/Kimi implementan UI contra un diseño aprobado; no sustituyen
   la decisión de diseño que el contrato asigna a Opus.
5. Añadir una excepción explícita de **control nativo de razonamiento para
   Go**: DeepSeek usa low/high; MiniMax tiene modos none/thinking; Kimi por la
   ruta Go examinada no ofrece variantes numéricas. Registrar modo efectivo y
   limitar pasos/duración. Esos límites no equivalen a un esfuerzo high.
   Para modelos con escala comparable, conservar el tope high. Sin esta
   excepción, MiniMax/Kimi no deben activarse bajo el contrato actual.
6. Añadir al routing el enlace condicional a `~/.agents/docs/opencode-workers.md`
   antes del primer despacho Codex/Claude Code → OpenCode Go.

La excepción del punto 5 es parte de lo que se propone aprobar; no está
aplicada. Su mapeo técnico y controles están en el [borrador de transporte](opencode-workers.md).

## Rama documental y activación posterior

| Destino tras aprobación | Cambio |
|---|---|
| `docs/agent-routing.md` | Tabla, tres presupuestos, excepción de esfuerzo y enlace a transporte. |
| `docs/opencode-workers.md` | Nueva guía común para llamadas programáticas. |
| `configs/opencode/` | Tres perfiles cheap-coder, modelo explícito y permisos por encargo. |
| `README.md` | Quinta ruta de ejecución e instalación de guía/perfiles; conservar distinción del MCP antiguo. |

El contrato común ya contempla trabajadores externos: no hace falta
reescribirlo ni duplicar la tabla en ambos entrypoints. La guía común se
instalaría en `~/.agents/docs/`; los perfiles en la configuración de OpenCode.

Para aceptar la activación: comprobar catálogo y opciones efectivos, ejecutar
un encargo pequeño por modelo, comprobar modelo usado y resultado, observar
una reanudación y una cancelación, y verificar que un perfil inválido no se
despacha con defaults. El orquestador revisará evidencia y diff completo.

Ahora sólo se ha validado documentación/código público: `opencode` no está
en el PATH de esta sesión. Existe configuración local, sin perfiles ni modelo
definidos en ese archivo. No se inspeccionaron credenciales ni saldo; no se
instaló, configuró ni ejecutó inferencia. Commit, push e instalación global
no realizados.
