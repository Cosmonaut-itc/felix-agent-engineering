# Flujo de trabajo

Este documento describe cómo trabaja Felix con agentes de código: qué capas
forman el flujo, qué decide cada una, qué skills cubren cada parte de una
tarea y qué hace el dueño. Es la fuente de verdad del flujo. El
[handbook](../handbook/index.html) lo resume para consulta rápida y el
[routing](agent-routing.md) decide modelos, esfuerzo y revisión.

## Capas

Cada capa vive en este repositorio o se instala desde un upstream que el
repositorio declara. Ante un conflicto manda la capa de más arriba.

| Capa | Qué decide | Fuente |
|------|------------|--------|
| El usuario | Objetivo, prueba esperada y autorizaciones de la sesión. | La conversación. |
| `AGENTS.md` del repositorio | Gates, tracker, skill `verify`, alto riesgo y project playbooks. | Cada repositorio de trabajo. |
| Contrato | Roles, Despachar → Recoger → Vetar → Cerrar y descendientes. | [`orchestration.md`](orchestration.md), inlineado en los entrypoints. |
| Routing | Modelo, esfuerzo, escalado, revisión, UI/UX y modelos por rol de pstack. | [`agent-routing.md`](agent-routing.md). |
| Entrypoints | Cargan el contrato y activan `felix-mode` con la sección «Flujo de ingeniería». | [`CLAUDE.md`](../configs/claude/CLAUDE.md) y [`AGENTS.md`](../configs/codex/AGENTS.md). |
| `felix-mode` | Cómo encajan pstack, el contrato y Matt Pocock: precedencia, sustituciones, revisión y pausas. | [`skills/felix-mode`](../skills/felix-mode/SKILL.md). |
| pstack | Playbooks de ejecución, principios y verificación sobre el artefacto real. | Plugin `pstack@pstack-claude`, del upstream [michael-denyer/pstack-claude](https://github.com/michael-denyer/pstack-claude). |
| Matt Pocock | Producto y planificación: entrevistas, specs, tickets y triage. | [`vendor/mattpocock`](../vendor/mattpocock/), copia del último release de [mattpocock/skills](https://github.com/mattpocock/skills). |
| Transporte | Cómo llega un encargo a otro runtime y cómo se recoge. | [`native-subagents.md`](native-subagents.md), [`codex-delegation.md`](../configs/claude/docs/codex-delegation.md), [`claude-workers.md`](../configs/codex/docs/claude-workers.md) y [`opencode-workers.md`](opencode-workers.md). |

La precedencia de `felix-mode` es la misma: usuario, `AGENTS.md` del
repositorio, contrato y routing, `felix-mode` y pstack.

## Cuándo se activa

La sección «Flujo de ingeniería» de cada entrypoint invoca `felix-mode` antes
de actuar en una tarea que cumpla cualquiera de estos criterios:

- toca más de un archivo;
- cambia una firma que otros llaman;
- implica una decisión de diseño;
- es un bug de causa desconocida o un problema de rendimiento.

Una pregunta trivial o un cambio contenido en un archivo con su test obvio se
hace directo y se verifica sobre el artefacto real. Un encargo delegado por
otro agente sigue el contrato como trabajador y no activa `felix-mode`, salvo
que el encargo lo pida.

## Recorrido de una tarea

| Parte | Qué pasa | Skills | Quién las invoca |
|-------|----------|--------|------------------|
| 1. Planificar | Opcional. Convierte una idea en un brief: spec, issues o mapa de decisiones. | `grilling`, `grill-me`, `grill-with-docs`, `domain-modeling`, `to-spec`, `to-tickets`, `triage`, `wayfinder`, `research`, `to-questionnaire` | Tú |
| 2. Afinar el pedido | Opcional. El agente reformula o afina la tarea antes de empezar. | `pstack:sharpen`, `pstack:in-your-own-words` | Tú |
| 3. Activar | Los criterios de arriba deciden si corre `felix-mode`. | `felix-mode`, `poteto-mode` | Agente |
| 4. Elegir playbook | Copia los pasos del playbook a la lista de tareas y aplica los project playbooks del repositorio. | `poteto-mode`, `figure-it-out` si ninguno encaja | Agente |
| 5. Entender | Explica el subsistema o la historia antes de cambiarlo. | `how`, `why` | Agente |
| 6. Diseñar | Tipos y firmas antes del código. Un runner opus-5.5 entrega dos candidatos y el orquestador elige. | `architect`, `arena` | Agente |
| 7. Implementar | Delega el código según el routing. En bugs, el test que falla va antes del arreglo. | `tdd`, `swarm` | Agente |
| 8. Medir | Antes de creer o reportar un número de rendimiento. | `benchmark-checklist` | Agente |
| 9. Verificar | Prueba sobre el artefacto real con la skill del repositorio. Si falta, sigue sus docs y propone crearla. | `verify` del repositorio, `create-verification-skill` | Agente |
| 10. Revisar | Revisor independiente gpt-6-astra en `high`. En alto riesgo, `interrogate` con astra y opus-5.5. | `interrogate` | Agente |
| 11. Abrir el PR | Limpia código y prosa y escribe título y descripción. | `deslop`, `no-comments`, `technical-writing`, `unslop` | Agente |
| 12. Llevar a merge | CI, comentarios y conflictos. Aterrizar un stack verde. | Playbooks Babysit y Shipping, `fix-ci`, `fix-merge-conflicts`, `get-pr-comments` | Tú lo pides |
| 13. Cerrar | Informe con evidencia y etiquetas: medido, inferido o suposición. Bitácora en trabajo largo. | `show-me-your-work` | Agente |

Los playbooks de pstack que más se usan son Bug fix, Perf issue, Hillclimb,
Feature, Refactoring, Prototype, Investigation, Babysit y Shipping. Cada uno
termina en «Opening a PR».

## Revisión

| Nivel | Cuándo | Cómo |
|-------|--------|------|
| Normal | Toda review de cambios. | Un revisor gpt-6-astra en `high`, con contexto nuevo y evidencia primaria. |
| Alto riesgo | Producción o sistemas de cliente, migraciones o borrado de datos, autenticación y permisos, dinero o impuestos, contratos entre servicios, o lo que el `AGENTS.md` marque. | `interrogate` con astra en `high` como Reviewer A y opus-5.5 en `high`. El orquestador veta los hallazgos. |
| Diseño | Decisiones de forma antes de implementar. | `architect` con un runner opus-5.5 y dos candidatos. Sin juez cruzado. |
| Diseño disputado | Alto riesgo, o dos candidatos empatados. | `arena` con opus-5.5 y gpt-6.1-sol, juzgado por astra, y dos implementaciones completas. |

Si no puede correr ningún revisor independiente, la puerta sigue cerrada. La
lectura propia del orquestador no cuenta como review.

## Pausas

El agente avanza sin preguntar en trabajo local y reversible. Pausa antes de:

- hacer push a `main` o mergear cuando eso dispare un deploy;
- escribir en ICG u otro sistema de un cliente en producción;
- actualizar el tracker más allá de lo que prescribe el repositorio;
- instalar dependencias, cambiar CI o tocar la configuración global de agentes;
- implementar UI/UX sin un prototipo aprobado;
- un force push, un deploy, un borrado de datos o un mensaje a un cliente.

## Lo que hace el dueño

- Da el objetivo y la prueba que quiere ver. Un issue `ready-for-agent`, una
  spec o un PRD sirven como brief.
- Aprueba las pausas.
- Lee el informe final, que nombra el nivel de revisión aplicado y por qué.
- Reinicia las sesiones abiertas después de una actualización de pstack.

## Skills que invoca el dueño

Estas skills no las dispara ningún playbook. Corren cuando las pides.

| Skill | Para qué |
|-------|----------|
| `pstack:sharpen` | Afina la tarea antes de empezar y después la hace. |
| `pstack:in-your-own-words` | Reformula tu objetivo para que lo confirmes. |
| `pstack:bro`, `wait-what` | Repiten el último mensaje en lenguaje llano. |
| `pstack:teach`, `teach` | Explican algo a fondo. La de pstack junta `how` y `why`. |
| `pstack:recall`, `pstack:ketchup` | Reconstruyen el contexto reciente o lo que pasó en el chat. |
| `pstack:what-did-i-get-done` | Resume tus commits de un periodo. |
| `pstack:blast-radius` | Qué más puede romper un cambio, comprobado con código. |
| `pstack:reflect`, `pstack:correct` | Convierten aprendizajes y errores repetidos en ediciones de skills, tipos o tests. |
| `pstack:automate-me` | Actualiza `felix-mode` desde tus transcripts. Pídele que conserve el `description` y no añada `disable-model-invocation`. |
| `pstack:maintain-verification-skill` | Audita que la `verify` de un repositorio siga diciendo la verdad. |
| `pstack:make-pr-easy-to-review` | Ordena commits y descripción de un PR. |
| `pstack:poteto-help`, `ask-matt` | Recomiendan qué skill o playbook usar. |
| `setup-matt-pocock-skills` | Configura un repositorio para las skills de planificación: tracker, etiquetas y docs de dominio. |
| `improve-codebase-architecture`, `codebase-design` | Buscan oportunidades de profundizar módulos y dan el vocabulario para diseñarlos. |
| `implement-spec`, `retro` | Implementan una spec y hacen la retrospectiva de una sesión, cuando el `AGENTS.md` del repositorio las pide. |
| `wizard` | Genera un asistente en bash para pasos que sólo puede hacer una persona. |

## Duplicados retirados

pstack cubre estas necesidades, así que sus equivalentes no se instalan.

| Necesidad | Se usa | Retiradas |
|-----------|--------|-----------|
| Test primero | `tdd` de pstack | `tdd` de Matt Pocock, `test-driven-development` |
| Bug | Playbook Bug fix | `diagnosing-bugs`, `systematic-debugging` |
| Prototipo | Playbook Prototype | `prototype` de Matt Pocock |
| Implementar un issue o una spec | Playbook Feature | `implement`, `subagent-driven-development`, `executing-plans` |
| Review de cambios | Sección Revisión | `code-review`, `pr`, `requesting-code-review` |
| Pausar o retomar | Playbooks Pause safely y Session pickup | `handoff` |
| Escribir skills o `AGENTS.md` | Playbook Authoring a skill y `unslop` | `writing-for-agents`, `writing-skills` |
| Conflictos de merge | `fix-merge-conflicts` | `resolving-merge-conflicts` |

Superpowers no se usa. Tampoco `setup-pstack`, que reescribe la hoja
`pstack-models.md` que genera el routing, ni `thermos` para revisar cambios.

## Proyecto

Cada repositorio de trabajo aporta lo que el flujo no puede saber:

- su `AGENTS.md`, con gates, tracker y señales de alto riesgo;
- la skill `verify` en `.agents/skills/verify/`, enlazada en
  `.claude/skills/verify`, que maneja la app real y devuelve evidencia;
- project playbooks opcionales en `.agents/playbooks/`, que extienden los de
  pstack.
