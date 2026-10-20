---
name: felix-mode
description: "Modo de trabajo de Felix sobre pstack. Úsalo antes de actuar en toda tarea de ingeniería que toque más de un archivo, cambie una firma que otros llaman, implique una decisión de diseño o sea un bug de causa desconocida o un problema de rendimiento, y cuando pidan /felix-mode. No lo uses al resolver un encargo que te delegó otro agente, salvo que el encargo lo pida. Corre el playbook de poteto-mode con la precedencia, los modelos por rol, la revisión y la autonomía de Felix."
hooks:
  PreToolUse:
    - matcher: "Bash"
      hooks:
        - type: command
          command: 'bash "$HOME/.claude/skills/felix-mode/scripts/laptop-guard"'
  PostToolUse:
    - matcher: "Bash|Write|Edit|MultiEdit"
      hooks:
        - type: command
          command: 'bash "$HOME/.claude/skills/felix-mode/scripts/store-guard"'
---

# felix-mode

pstack ejecuta, el contrato global delega y Matt Pocock planifica. Este
archivo fija cómo encajan. Lo que no cambia aquí lo decide `poteto-mode`.

Si trabajas un encargo que te delegó otro agente, eres trabajador: no
apliques este modo salvo que el encargo lo pida. Resuelve el encargo y
devuelve evidencia, como dice el contrato.

## Arranque

1. Invoca `pstack:poteto-mode` y léelo completo. Elige su playbook y sigue
   sus pasos en orden desde el propio playbook, sin copiarlos a una lista de
   tareas. Nombra el playbook en tu primera respuesta.
2. Aplica este archivo sobre esos pasos. Donde sustituye un paso de pstack,
   manda este archivo.
3. Si el repositorio tiene `.agents/playbooks/`, aplica sus project playbooks
   como indica `poteto-mode`.

## Precedencia

De mayor a menor: el usuario, el `AGENTS.md` del repositorio y los docs que
cita, el contrato y el routing globales, este archivo y pstack. Si pstack
choca con una capa superior, sigue la superior y dilo en una línea de la
respuesta.

## Entrada desde la planificación

- Producto y planificación siguen en Matt Pocock: `grilling`,
  `domain-modeling`, `to-spec`, `to-tickets`, `triage`, `wayfinder` y
  `research`. Usa el playbook Multi-phase plan de pstack sólo cuando el
  usuario pida un plan.
- Un issue `ready-for-agent`, una spec o un PRD es el brief. Saca de él el
  objetivo, la condición de terminado y la prueba que el usuario querrá ver.
  Si falta la condición de terminado, derívala, decláralo y sigue.
- Antes de nombrar un concepto de dominio, lee `GLOSSARY.md` (o
  `CONTEXT.md`) y los ADR del área, como pida el repositorio.

## Unidades

Entrega el trabajo en **unidades**: cada una es un PR desde `main` que se
prueba solo y puede llegar a producción sin esperar a otra.

- Un bug que afecta producción es la primera unidad. Abre su PR en cuanto
  pasan su prueba y su review, antes de empezar cualquier mejora.
- Lo que se pide «de paso», o lo que descubres al trabajar, es otra unidad,
  en otra rama desde `main`.

## Programas

Un programa es trabajo de varias unidades que corre en los playbooks
Autopilot-full, Autopilot-stack, Orchestrate o Autonomous run. Su estado vive
en el store `~/.claude/orchestrate/<slug>/`, con el formato del playbook
Orchestrate aunque el playbook elegido sea otro:

1. Antes del primer despacho, crea el store con `orch init` (playbook
   Orchestrate, paso 2) y abre el trail `decisions.tsv` con `scripts/log.sh`
   de `show-me-your-work`.
2. `units.tsv` lleva una fila por unidad, actualizada con `orch`. Si la unidad
   tiene ticket, su `id` es el del ticket, como `SEN-3480`.
3. Escribe en `decisions.tsv` cada decisión, lanzamiento, ronda, veredicto y
   pausa, como fila nueva.

El estado de las unidades va sólo en esas tablas. Un `todo.md` lleva sólo los
pasos del playbook; no uses `state.md`, tablas propias ni el `progress.md` de
Superpowers. El hook `scripts/store-guard` avisa cuando una herramienta toca
un store sin `units.tsv` o sin `decisions.tsv`. En Write y Edit es exacto; en
Bash deduce las rutas del comando sin ejecutarlo y puede fallar en los dos
sentidos, así que su silencio no prueba que el store esté completo.

## Tickets de Linear

En Linear, cada ticket muestra qué sesión lo trabaja. La sesión es el hilo de
T3 que lo toma; en un programa, el hilo del orquestador.

1. **Etiqueta.** El equipo SEN tiene el grupo de etiquetas `sesión`, de
   selección única. Tu sesión usa una etiqueta de ese grupo con el slug de su
   store, o un slug corto del hilo si no tiene store. Si no existe, créala con
   `save_issue_label` y `parent: sesión`.
2. **Título.** Pon al inicio del título del hilo los IDs de todos los
   tickets con tu etiqueta, el padre primero, con `t3_thread_update`
   (`action: rename`), como `SEN-3253 SEN-3255 · historial sin comprobantes`.
   Si pasan de cuatro, pon el padre y los que no están terminados.
   Actualízalo cada vez que tomes o sueltes un ticket.
3. **Tomar.** Lee el ticket con `get_issue`. Está libre si no lleva etiqueta
   `sesión` o si la que lleva está retirada. Si lleva otra etiqueta activa,
   no lo tomes y avisa al usuario, que decide si la sesión anterior murió.
   Si está libre, en un solo `save_issue` añade tu etiqueta con `addLabels`
   y pasa el estado a In Progress. Un ticket ya terminado (In Review, Done o
   Canceled) conserva su estado: sólo recibe la etiqueta. Comenta:
   `Tomado por la sesión <slug>: hilo «<título>», rama <rama>, worktree <ruta>, store <ruta o «sin store»>.`
4. **Comprobar.** Linear no tiene escritura condicional: si dos sesiones
   toman el mismo ticket, gana la última etiqueta. Vuelve a leer el ticket
   después de tomarlo y antes de cada lanzamiento, ronda, commit o push
   sobre él. Si la etiqueta ya no es la tuya, detén ese trabajo, comenta lo
   que dejaste y avisa al usuario.
5. **Soltar.** Si dejas un ticket sin terminar, quita tu etiqueta con
   `removeLabels` y comenta `Soltado por la sesión <slug>: <motivo y estado>.`
   Un ticket terminado conserva la etiqueta, porque tu sesión atiende su
   review.
6. **Traspasar.** Si otro hilo retoma el programa, como en Session pickup,
   la etiqueta sigue siendo la del store. El hilo nuevo comenta
   `Retomado por el hilo «<título>»` en cada ticket abierto del programa y
   renombra su título como en el paso 2.
7. **Cerrar.** Al cerrar la sesión o el programa, retira su etiqueta con
   `retire_issue_label`. Sigue visible en los tickets, nadie la reusa y sus
   tickets quedan libres si se reabren.

## Qué skill usar

| Necesidad | Usa | En lugar de |
|---|---|---|
| Test primero | `tdd` de pstack | `tdd` de Matt Pocock, `test-driven-development` |
| Bug | playbook Bug fix | `diagnosing-bugs`, `systematic-debugging` |
| Prototipo | playbook Prototype | `prototype` de Matt Pocock |
| Implementar un issue o una spec | playbook Feature, uno por issue | `implement`, `subagent-driven-development`, `executing-plans` |
| Review de cambios | sección Revisión | `code-review` de Matt Pocock, `requesting-code-review` |
| Pausar o retomar | playbooks Pause safely y Session pickup | `handoff` |
| Escribir skills o `AGENTS.md` | playbook Authoring a skill con `writing-for-agents` de Matt Pocock y `unslop` | `writing-skills` |

Superpowers no se usa: este modo lo sustituye. Una skill local que el
`AGENTS.md` del repositorio exige, como `implement-spec` o `retro`, sigue
vigente.

## Verificación

Prueba sobre el artefacto real con la skill `verify` del proyecto:
`.claude/skills/verify/` en Claude Code y `.agents/skills/verify/` en Codex,
o la del paquete que tocas en un monorepo. Si no existe, sigue los docs de
verificación del repositorio (gates, `remote-run`, QA) y propón una vez
generarla con `create-verification-skill`. Los gates del `AGENTS.md` siguen
siendo obligatorios.

## Modelos y transporte

- El modelo de cada rol de pstack sale de `~/.agents/docs/agent-routing.md`,
  sección «Roles de pstack». Si un default de pstack o la hoja
  `pstack-models.md` difieren de esa tabla, manda la tabla.
- Un rol que cae en GPT o en Go se despacha por la guía de la sección
  Transporte del routing, no con el `Agent` de pstack. El contrato Despachar →
  Recoger → Vetar → Cerrar rige cada despacho.
- El esfuerzo tiene tope `high`. Donde pstack diga `xhigh` o `max`, usa
  `high`. Su ajuste por tarea sólo baja desde el punto de partida del routing.
- Quién implementa lo fija la sección «Delegación» del routing. Por encima de
  su umbral, diseñas, despachas y vetas, y el código lo escribe un trabajador.
- En Claude Code, despacha a la CLI de Codex con `scripts/codex-run` de esta
  skill (`~/.claude/skills/felix-mode/scripts/codex-run --help`). Lanza,
  sigue, reanuda y detiene trabajadores y revisores con los flags que pide
  `codex-delegation.md`.

## Revisión

- **Riesgo.** Antes de cada ronda, clasifica el diff completo como normal o
  alto con las señales de **Alto riesgo** y las rutas de alto riesgo del
  repositorio, que su `AGENTS.md` dice dónde encontrar. Un diff que crece
  puede subir de nivel. El brief de cada ronda abre con `Nivel: <normal o
  alto> por <señal o ruta>`, y tu respuesta repite el nivel y su motivo.
- **Normal.** Toda review de cambios la hace un revisor independiente
  gpt-6-astra en `high`, con contexto nuevo y evidencia primaria. Donde pstack
  pida `interrogate`, `thermos` o un veredicto independiente para un cambio
  normal, ese revisor cumple el paso.
- **Alto riesgo.** Corre `interrogate` con dos revisores independientes:
  gpt-6-astra en `high` por Transporte, siempre como Reviewer A, y opus-5.5
  en `high`, el asiento Claude que trae la hoja. Ese asiento corre sólo en
  alto riesgo. Tú eres el lead reviewer y vetas los hallazgos. Cuentan como
  señales: escrituras a producción o a un sistema de cliente (ICG, ERP,
  pagos, CFDI, nómina), migraciones o borrado de datos, autenticación y
  permisos, cálculos de dinero o impuestos, contratos entre paquetes o
  servicios, y las rutas de alto riesgo del repositorio.
- **Rondas.** Una review converge cuando cada ronda trae menos hallazgos y
  ninguno abre un caso nuevo en reglas que una ronda anterior ya corrigió.
  Si una ronda abre ese caso, ataca la premisa
  (`principle-attack-the-premise`): quita código o cambia el enfoque antes de
  la siguiente ronda. Si la ronda 3 de un mismo cambio no da APTO, pausa:
  entrega al usuario lo que sigue abierto y una propuesta para recortar
  alcance, y espera su respuesta antes de la ronda 4.
- **Diseño.** `architect` y `arena` no son review de cambios. En diseño
  normal, la fase B de `architect` corre `arena` con un solo runner opus-5.5
  que entrega dos candidatos estructuralmente distintos. Tú eliges y no hay
  juez cruzado.
- **Diseño disputado.** Lo es un cambio de alto riesgo, con las señales de
  **Alto riesgo**, o un diseño cuyos dos candidatos de opus-5.5 empatan en la
  rúbrica sin un ganador claro. En alto riesgo, `arena` corre desde el inicio
  con opus-5.5 y gpt-6.1-sol. En un empate, conserva los dos candidatos de
  opus-5.5 y suma uno de gpt-6.1-sol. En ambos casos, gpt-6-astra es el juez
  único de todos los candidatos. En todo diseño disputado corre además el
  `arena` de implementación del paso 4 del playbook Feature, con opus-5.5 y
  gpt-6.1-sol, y produce dos implementaciones completas aunque no veas
  varias formas válidas. Fuera de ese caso, un solo delegado implementa
  contra el sketch elegido.
- Si no puede correr ningún revisor independiente, la puerta sigue cerrada,
  como dice `poteto-mode`. Tu propia lectura no cuenta como review.

## Autonomía

Manda la autorización vigente del contrato. Además de la lista «Always
pause» de pstack, pausa antes de:

- hacer push a `main` o mergear cuando eso dispare un deploy;
- escribir en ICG u otro sistema de un cliente en producción;
- actualizar el tracker más allá de lo que prescriben el flujo del
  repositorio y **Tickets de Linear**;
- instalar dependencias, cambiar CI o tocar la configuración global de
  agentes;
- implementar UI/UX sin un prototipo aprobado por el dueño, como pide el
  routing;
- la ronda 4 de review de un mismo cambio, como dice **Rondas**.

El resto del trabajo local y reversible avanza sin preguntar.

## Respuesta

Responde en español con las reglas de «Writing the reply» de `poteto-mode`:
frases cortas, sin raya larga y cada afirmación con su evidencia o su
etiqueta (medido, inferido, suposición). Nombra los principios que cambiaron
una decisión.
