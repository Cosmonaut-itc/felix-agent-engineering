# Delegación y elección de modelo

Política común para seleccionar trabajadores y revisores.

## Modelos

Elige siempre dentro de esta tabla; los entrypoints globales llevan una
copia, así que al cambiarla replícala en ambos. Las puntuaciones son preferencias
operativas (10 = mejor; en costo, más económico) y se ajustan con resultados
observados. Inteligencia y gusto de los modelos Go, de gpt-6.1-sol,
gpt-6-luna, sonnet-5.5 y haiku-5.5, son hipótesis iniciales,
pendientes de calibrar con encargos propios. Gusto valora criterio de
producto, claridad y coherencia visual; admitir imágenes no demuestra buen
gusto.

| Modelo        | costo | inteligencia | gusto | Cuándo |
|---------------|-------|--------------|-------|--------|
| gpt-6-astra   | 5     | 10           | 7     | Problemas más complejos que Sol no resolvió o que claramente lo superan; reviews de cambios en high. |
| gpt-6.1-sol   | 9     | 9            | 6     | Opción habitual para ejecución técnica. |
| gpt-6-luna    | 10    | 7            | 4     | Primera opción Codex para encargos claros y fáciles de verificar; cuesta la mitad que gpt-5.6-luna. |
| opus-5.5      | 8     | 9            | 9     | Gusto visual y decisiones de producto. |
| sonnet-5.5    | 6     | 7            | 7     | Trabajo en el entorno Claude que no requiera el juicio de Opus. |
| haiku-5.5     | 10    | 6            | 4     | Exploración, lectura y extracción de alto volumen, y encargos mecánicos fáciles de verificar en el entorno Claude. |
| DeepSeek V4.1 Flash · Go | 10* | 8 | 6 | Primera opción económica para encargos técnicos acotados y verificables. |
| MiniMax M3 · Go | 9 | 8 | 7 | Implementación frontend/backend y trabajo visual contra un diseño aprobado. |
| Kimi K3 · Go | 6 | 9 | 8 | Escalón de mayor capacidad en Go para encargos complejos y contexto amplio; reservar cuota para trabajo que lo justifique. |

*DeepSeek recibe costo 10 mientras esté vigente la promoción 4×; fuera de
ella, costo 8 sujeto a tarifas y cuota actuales. Verifica la promoción antes
de usar esa ventaja para elegirlo; si no puedes confirmarla, usa costo 8.
Consulta [tarifas y cuota Go](opencode-workers.md#tarifas-y-cuota).

Ninguna ruta es gratis. Administra **tres presupuestos limitados: Codex,
Claude y Go**. Los modelos de una misma suscripción Go comparten cuota:
cambiar de perfil no recupera presupuesto agotado. Considera cuota disponible,
tarifa, caché y reintentos al estimar el coste del encargo completo.

gpt-6-luna cuesta la mitad que gpt-5.6-luna, al que sustituye. Dentro del
presupuesto Codex, gpt-6-luna y gpt-6.1-sol son el punto de partida para
ejecución: luna si el encargo es claro y verificable, sol si pide más criterio.

Para ejecución, parte del modelo más barato que plausiblemente dé la talla y
sube por escalado en vez de abrir con el más capaz; los pases obligatorios de
revisión siguen su propia sección. Gasta Claude donde el gusto, el entorno o
una perspectiva independiente pagan. Con capacidad adecuada y cuota
disponible, prefiere Go para ejecución acotada. Elige entre sus tres modelos
por el encargo; no recorras los tres como una cadena obligatoria de reintentos.

Compara el costo del despacho completo, no el precio nominal del modelo: un
modelo capaz en `low` puede salir más barato que uno económico en `high` para
el mismo resultado, y un despacho barato que hay que repetir cuesta más que
el caro que cierra. En conflicto sobre algo que se embarca: **inteligencia >
gusto > costo**. La elección explícita del usuario prevalece.

El MCP antiguo `cheap-coder` (`stealth/ox-alpha`) sigue **deprecado**. Los
nuevos cheap coders de esta tabla usan OpenCode Go por la ruta documentada
abajo; incluirlos no reactiva aquel MCP.

## Esfuerzo

Selecciona modelo y esfuerzo por separado, usando sólo niveles admitidos por
cada modelo. El esfuerzo sigue la incertidumbre del encargo, no su tamaño.

| Esfuerzo | Criterio |
|----------|----------|
| low | Contexto suficiente para una solución directa y una comprobación sencilla. |
| medium | Hace falta interpretar, planificar o comparar alternativas; punto de partida habitual. |
| high | Persisten incertidumbres importantes o se necesita razonamiento profundo. |

**Fuera de reviews de cambios, gpt-6-astra se calibra un escalón por debajo.**
Astra en low rinde más que gpt-5.6-sol en high, según
[OpenAI](https://x.com/thsottiaux/status/2096688770523467947).
Con Astra, `low` es el punto de partida habitual y `medium` cubre lo que con
gpt-5.6-sol pedía `high`; reserva `high` para incertidumbres que medium no
resolvió. Sol y Astra se distinguen por el tipo de problema, no por
equivalencias de esfuerzo: al pasar de Sol a Astra, elige el esfuerzo de
Astra por la incertidumbre que queda.

El tope de cualquier despacho con escala de esfuerzo comparable, aunque
reanude, es **high**. Esto incluye trabajadores, revisores, adaptadores y
descendientes GPT/Claude.

Configura el esfuerzo explícitamente en el runtime; un default de
configuración no es un tope y pedirlo en el prompt no lo fija. Si se hereda,
comprueba el valor efectivo. Si la ruta no permite fijarlo, usa otra ruta o
resuelve directamente, salvo la excepción Go siguiente.

**Go:** DeepSeek y Kimi K3 parten de low y escalan a high; ambos conservan el
tope high. K3 está habilitado con variantes explícitas low/high. Se verificó
el envío y la aceptación de ambos valores por Go; el nivel interno aplicado
no tiene confirmación observable. El dueño autorizó su uso con esta limitación
y evaluación mediante encargos reales. K3 sustituye al perfil K2.7 de modo fijo.

**Excepción Go para MiniMax M3:** usa none/thinking. Configura y registra el
modo nativo soportado y el presupuesto de pasos/duración indicado en su guía.
No etiquetes un modo adaptativo como high ni confundas pasos con tokens. Si
el control efectivo no puede verificarse, la ruta sigue pendiente de
validación. Esta excepción no amplía el tope de los modelos que sí admiten
una escala comparable.

## Escalado

Tienes **permiso permanente** de escalar a un modelo o esfuerzo mejor cuando
el output no da la talla, sin preguntar. Escala ante contradicciones,
hipótesis pendientes o criterios de aceptación incumplidos por razonamiento;
entrega al nuevo intento la evidencia útil del anterior. En Codex, el paso
habitual es de Sol a Astra cuando el problema resultó más complejo de lo que
Sol pudo resolver.

La falta de contexto, acceso o herramientas se resuelve aparte, no escalando.
Si ninguna opción de la tabla basta, devuelve el estado parcial y la decisión
pendiente.

## Revisión y UI/UX

Las reviews de cambios las hace un revisor independiente con gpt-6-astra en
esfuerzo `high`. En un cambio de alto riesgo, según lo juzgue el orquestador,
se suma opus-5.5 en `high` como segundo revisor independiente. Ningún otro
modelo revisa cambios. El orquestador veta el cambio y los hallazgos.

UI/UX: primero un prototipo de fidelidad baja o media aprobado por el dueño.
Si faltan prototipo y guía, diseña opus-5.5. Implementa con los criterios
generales y verifica contra el diseño y sus estados de interacción.

## Roles de pstack

pstack asigna un modelo a cada rol. Esta tabla fija el punto de partida de
cada rol con los nombres de la hoja `pstack-models.md`, y sustituye los
defaults de pstack. A partir de ahí rigen Esfuerzo y Escalado.

| Rol de pstack | Punto de partida |
|---------------|------------------|
| `feature, refactoring`, `bug-fix`, `perf-issue`, `hillclimb` | Selección normal de ejecución según Modelos. En el entorno Claude, sonnet-5.5. |
| `strongest judgment` | gpt-6-astra en `low`; opus-5.5 si la decisión es de producto. |
| `judgment and prose` | El orquestador (`inherit-parent`). |
| `how explorer`, `why investigators`, `swarm workers`, `reflect tooling` | haiku-5.5 en `low`, o DeepSeek en Go. |
| `how explainer`, `why synthesizer`, `reflect judgment, divergent, synthesizer` | sonnet-5.5 en `medium`. |
| `architect runners`, `arena runners` | opus-5.5; en diseño disputado (alto riesgo o empate entre los candidatos de opus-5.5, según `felix-mode`), también gpt-6.1-sol. |
| `arena cross-judge pool` | Familia distinta del autor: gpt-6-astra si el autor es Claude, opus-5.5 si es GPT. Un panel mixto opus-5.5 y gpt-6.1-sol tiene un juez único, gpt-6-astra. |
| `interrogate reviewers` | Según Revisión: gpt-6-astra en `high`; en alto riesgo, también opus-5.5 en `high`. |
| Revisor del rastro de `show-me-your-work` | gpt-6-astra. |

El `Agent` de pstack en Claude Code sólo acepta modelos Claude, así que la
hoja `~/.claude/pstack-models.md` lleva el asiento Claude de cada rol. Un rol
en GPT o en Go se despacha por Transporte, y su fila de la hoja sólo corre
cuando esta tabla pide Claude.

## Transporte

Antes del primer despacho por una ruta en la sesión, lee su guía:

| Ruta | Guía |
|------|------|
| Codex → GPT (subagentes nativos) | `~/.agents/docs/native-subagents.md`, rama Codex |
| Claude Code → Claude (subagentes nativos) | `~/.agents/docs/native-subagents.md`, rama Claude Code |
| Codex → Claude (CLI externo) | `~/.codex/docs/claude-workers.md` |
| Claude Code → GPT (plugin o CLI) | `~/.claude/docs/codex-delegation.md` |
| Codex o Claude Code → OpenCode Go (CLI externo o HTTP) | `~/.agents/docs/opencode-workers.md` |

Las guías determinan mecanismos y parámetros; este documento determina la
selección. Comunica cualquier sustitución de modelo o esfuerzo.
