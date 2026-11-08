---
name: felix-mode
description: "Modo de ingeniería de Felix sobre pstack. Úsalo antes de actuar en una tarea que toque más de un archivo, cambie una firma que otros llaman, implique una decisión de diseño, o sea un bug de causa desconocida o un problema de rendimiento. Un encargo que te delegó otro agente lo usa sólo si lo pide."
---

# felix-mode

pstack ejecuta, el contrato global delega y Matt Pocock planifica. Este
archivo fija cómo encajan. Lo que no cambia aquí lo decide `poteto-mode`.

Si otro agente te delegó un encargo, eres trabajador. Resuelve el encargo y
devuelve evidencia, como dice el contrato, y aplica este modo sólo si el
encargo lo pide.

## Arranque

1. Invoca `pstack:poteto-mode` y léelo completo. Elige su playbook y sigue
   sus pasos en orden desde el propio playbook. La única copia de esos pasos
   es el `todo.md` que `poteto-mode` pide cuando la sesión no tiene
   herramienta de tareas. Nombra el playbook en tu primera respuesta.
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

Entrega el trabajo en **unidades**. Una unidad es un PR hacia `main`, o hacia
la rama de entrega del repositorio, que se prueba solo y puede llegar a
producción sin esperar a otra.

Si una spec tiene tickets que necesitan salir juntos por un contrato,
schema o migración compartidos, sigue el
[flujo de acumulador](references/acumulador.md). El conjunto es una unidad
de entrega y cada ticket conserva su prueba y review. El tamaño por sí solo
no pide acumulador. Los tickets independientes siguen en PRs separados.

- Un bug que afecta producción es la primera unidad. Abre su PR en cuanto
  pasan su prueba y su review, antes de empezar cualquier mejora.
- Lo que se pide «de paso», o lo que descubres al trabajar, es otra unidad,
  en otra rama desde `main`.

## Programas

Un programa es trabajo de varias unidades que corre en los playbooks
Autopilot-full, Autopilot-stack, Orchestrate o Autonomous run. Su estado vive
en el store `~/.claude/orchestrate/<slug>/`, con este formato aunque el
playbook use otro. No depende de scripts de pstack:

1. Antes del primer despacho, crea `units.tsv` con la cabecera
   `id	track	state	branch	pr	sha	brief`, separada por tabuladores, y abre
   el trail `decisions.tsv` con `scripts/log.sh` de `show-me-your-work`.
2. `units.tsv` lleva una fila por unidad con esas siete columnas, y la
   editas en su lugar. Si la unidad tiene ticket, su `id` es el del ticket,
   como `SEN-3480`.
3. Escribe en `decisions.tsv` cada decisión, lanzamiento, ronda, veredicto,
   merge y pausa, como fila nueva.

El estado de las unidades va sólo en esas tablas. Un `todo.md` lleva sólo los
pasos del playbook. El store es sólo de programas. Una unidad suelta guarda
sus encargos, salidas de trabajadores y parches en
`~/.claude/encargos/<slug>/`, sin crear store.

El hook `scripts/store-guard` avisa cuando una herramienta toca un store sin
`units.tsv` o sin `decisions.tsv`. En Write y Edit es exacto; en Bash deduce
las rutas del comando sin ejecutarlo y puede fallar en los dos sentidos, así
que su silencio no prueba que el store esté completo. Él,
`scripts/laptop-guard` y `scripts/merge-guard` se registran en
`~/.claude/settings.json`, como dice el README, porque T3 Code no ejecuta los
hooks del frontmatter de una skill.

## Tickets de Linear

Antes de tomar, comprobar, soltar o traspasar un ticket de Linear, y al
cerrar una sesión o un programa que tomó tickets, sigue
[Tickets de Linear](references/linear.md).

## Qué skill usar

| Necesidad | Usa | En lugar de |
|---|---|---|
| Test primero | `tdd` de pstack | `tdd` de Matt Pocock, `test-driven-development` |
| Bug | playbook Bug fix | `diagnosing-bugs`, `systematic-debugging` |
| Prototipo | playbook Prototype | `prototype` de Matt Pocock |
| Diseñar, implementar o revisar UI/UX | [Menos es más](references/menos-es-mas.md), también en el brief de cada trabajador | — |
| Implementar un issue o una spec | playbook Feature, uno por issue | `implement`, `subagent-driven-development`, `executing-plans` |
| Implementar tickets acoplados | [Acumulador](references/acumulador.md) | `implement-spec` de Matt Pocock |
| Review de cambios | sección Revisión | `code-review` de Matt Pocock, `requesting-code-review` |
| Tres modelos, cuando el usuario pide «consejo» | [Consejo](references/consejo.md) | `council` de tstack |
| Pausar o retomar | playbooks Pause safely y Session pickup; `handoff` de Matt Pocock cuando pides un documento de traspaso | — |
| Escribir skills o `AGENTS.md` | playbook Authoring a skill con `writing-for-agents` de Matt Pocock y `unslop` | `writing-skills` |

Este modo sustituye a Superpowers. Una skill local que el `AGENTS.md` del
repositorio exige, como `implement-spec` o `retro`, sigue vigente.

## Verificación

Prueba sobre el artefacto real con la skill `verify` del proyecto, que vive
en `.claude/skills/verify/` en Claude Code y en `.agents/skills/verify/` en
Codex, o con la del paquete que tocas en un monorepo. Si no existe, sigue los
docs de verificación del repositorio (gates, `remote-run`, QA) y propón una
vez generarla con `create-verification-skill`. Los gates del `AGENTS.md` siguen
siendo obligatorios.

Los hosts de pruebas son compartidos y hacen fila. Elige cada corrida por lo
que demuestra:

- **Prueba dirigida.** Mientras implementas, en cada ronda de corrección y
  en cada candidato de `arena`, corre sólo lo que toca el cambio: los tests
  de los archivos y paquetes modificados, el typecheck y el lint de esos
  paquetes, y la reproducción de cada hallazgo.
- **Suite completa.** Los gates del `AGENTS.md` corren sobre el head que
  recibió APTO, antes del merge. En `arena` corren sólo sobre la base
  elegida después del injerto, y en el acumulador en su paso 8. Si fallan,
  corrige con prueba dirigida; el head nuevo pasa por una ronda delta y
  la suite vuelve a correr sobre él.
- **Evidencia por instantánea.** Una corrida verde acredita lo que corrió:
  su SHA con el árbol limpio, sin cambios sin commitear, y las versiones de
  runtime, lockfiles y variables que leen los gates. Si cambió alguna de
  esas entradas, la evidencia ya no vale y la corrida se repite. Revisores
  y verificadores leen esa evidencia y corren sólo los comandos de sus
  hallazgos o de su verificación del artefacto. Repetir una suite sobre la
  misma instantánea sólo sirve para diagnosticar un fallo intermitente, y
  el brief lo declara.
- **Alcance en el nombre.** Cada corrida en un host de pruebas empieza su
  nombre con su alcance: `dirigida-`, `suite-`, `repro-` o `diag-`, seguido
  del ticket. Con `remote-run`, va en `--name`, como
  `--name suite-sen-5194`.
- **Fail-fast por etapa.** Ordena las etapas de la más barata a la más
  cara. Detén la corrida al terminar la primera etapa con fallos que la
  rama de entrega no trae. Dentro de cada etapa conserva la lista completa
  de fallos para compararla con la de esa rama.

## Modelos y transporte

- El modelo de cada rol de pstack sale de `~/.agents/docs/agent-routing.md`,
  sección «Roles de pstack». Si un default de pstack o la hoja
  `pstack-models.md` difieren de esa tabla, manda la tabla.
- Un rol que cae en GPT o en Go se despacha por la guía de la sección
  Transporte del routing. El contrato Despachar → Recoger → Vetar → Cerrar
  rige cada despacho.
- El esfuerzo tiene tope `high`. Donde pstack diga `xhigh` o `max`, usa
  `high`. Su ajuste por tarea sólo baja desde el punto de partida del routing.
- Quién implementa lo fija la sección «Delegación» del routing. Por encima de
  su umbral, diseñas, despachas y vetas, y el código lo escribe un trabajador.
- En Claude Code, despacha a Codex con `delegate_task` de T3 Code cuando
  está disponible, como define `codex-delegation.md`. Si no, usa
  `scripts/codex-run` de esta skill
  (`~/.claude/skills/felix-mode/scripts/codex-run --help`) para lanzar,
  seguir, reanudar y detener trabajadores y revisores con los flags que pide
  esa guía.

## Revisión

- **Riesgo.** Antes de cada ronda, trae la rama de entrega y corre
  `scripts/risk-classify` de esta skill desde el checkout del cambio.
  Compara con las pathspec `:(glob)` de `.agents/high-risk-paths`, una por
  línea, lo que cambiaron desde el merge-base HEAD, el stage, el working
  tree y esa rama, y marca `rename de directorio` cuando git podría mover un
  archivo; su `--help` da la sintaxis. Si imprime `Nivel: alto`, el nivel es
  alto. Si sólo lista archivos que la rama no tocó, o un `rename de
  directorio`, actualiza la rama con el destino y vuelve a correrlo. Si imprime `Rutas:`, clasifica
  el diff completo con las demás señales de **Alto riesgo**. Si sale con 2,
  corrige lo que dice antes de la ronda; un error no cuenta como normal. Un diff que crece puede subir de
  nivel. El brief de cada ronda abre con `Nivel: <normal o alto> por <señal
  o ruta>`, y tu respuesta repite el nivel y su motivo.
- **Pre-vuelo.** Antes de cada ronda, atiende los comentarios abiertos del
  PR, corrige lo que ya sabes que falla y corre la prueba dirigida del
  cambio. El brief nombra los checks que faltan o que ya fallaban.
- **Normal.** Toda review de cambios la hace un revisor independiente
  gpt-6.1-sol en `high`, con contexto nuevo y evidencia primaria. Donde pstack
  pida `interrogate`, `thermos` o un veredicto independiente para un cambio
  normal, ese revisor cumple el paso.
- **Alto riesgo.** Corre `interrogate` con dos revisores independientes:
  gpt-6.1-sol en `high` por Transporte, siempre como Reviewer A, y opus-5.5
  en `high`, el asiento Claude que trae la hoja. Ese asiento corre sólo en
  alto riesgo. Vetas sus hallazgos con **Prueba**. Cuentan como
  señales: escrituras a producción o a un sistema de cliente (ICG, ERP,
  pagos, CFDI, nómina), migraciones o borrado de datos, autenticación y
  permisos, cálculos de dinero o impuestos, contratos entre paquetes o
  servicios, y las rutas de `.agents/high-risk-paths`.
- **Prueba.** El brief de todo revisor pide que cada critical o warning
  traiga el comando que corrió, la salida que imprimió y la salida que exige
  la intención, como el brief de `interrogate`. En un hallazgo de
  documentación, el comando muestra las líneas que se contradicen. En el
  veto vuelves a correr cada comando. Un critical o warning sin comando, o
  cuyo comando no reproduce el fallo, no queda abierto.
- **Rondas.** Una review converge cuando cada ronda trae menos hallazgos y
  ninguno abre un caso nuevo en reglas que una ronda anterior ya corrigió.
  Si una ronda no converge, o la ronda 3 no da APTO, lee y aplica
  `principle-attack-the-premise` de pstack y **Autonomía** antes de la
  siguiente corrección. Un mismo cambio tiene hasta ocho rondas sin
  autorización adicional, contando las reviews delta y las renovaciones
  por cambio de head. Antes de la ronda 9 pausa, aunque la ronda 8 haya
  dado APTO. Entrega al usuario lo que sigue abierto y el replanteamiento
  realizado.
- **Hallazgos.** Corrige cada hallazgo aceptado con el cambio mínimo y
  rebate con evidencia el que esté mal. Lo que pide trabajo fuera del
  objetivo sigue **Unidades** y el [procedimiento de merge](references/merge.md).
  Declina todos los nits, también los de comentarios, documentación o
  estilo. No impiden APTO, y cada cambio de head exige otra ronda. Un critical o warning nunca
  es un nit, aunque trate de documentación.
- **Registro.** Lleva cada hallazgo en `hallazgos.tsv`, junto a los
  encargos de la unidad o en el store del programa, con las columnas
  `id	ronda	severidad	estado	evidencia`. El `id` no cambia entre rondas.
  El estado es `abierto`, `corregido`, `rebatido` o `declinado`. Desde la
  ronda 2, el brief suma el registro y el diff desde la ronda anterior, y
  el revisor marca cada id como `resuelto` o `sigue abierto: <motivo>`.
- **Regla por hallazgo.** Si un hallazgo aceptado es mecánico, como un
  patrón de sintaxis, una API prohibida, una forma de import o una regla de
  ubicación o de nombre de archivo, el mismo PR añade lo que lo impide. Sigue
  el orden de `pstack:correct`: arquitectura, tipos, un lint cuyo error
  nombra el arreglo, y por último un test. Muestra en la verificación del PR
  que la regla falla sobre el código anterior al arreglo y pasa después. Si
  la regla sale del objetivo del PR, o pide instalar dependencias o cambiar
  CI, no la metas en el PR. Abre un issue con el hallazgo y la regla
  propuesta, y la regla sigue **Unidades**. El hallazgo se corrige igual en
  el PR. Un hallazgo de juicio no lleva regla.
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
- **Sin revisor.** Si no puede correr ningún revisor independiente, la
  puerta sigue cerrada, como dice `poteto-mode`. Tu propia lectura no cuenta como review.
- **Constancia.** El cuerpo de cada PR termina con una línea por revisor
  exigido, con la forma
  `Revisión: <modelo> <esfuerzo>, ronda <n>, <veredicto> sobre <head>`.
  Copia el veredicto literal, también NO APTO o SIN DICTAMEN. Mientras el
  head del PR no tiene veredicto, `<veredicto>` es `pendiente` y `<n>` es
  la ronda que lo juzgará. En un acumulador sólo cuenta la review final del
  conjunto. Hasta entonces la línea dice `pendiente`, con los revisores del
  nivel vigente. Quien abre el PR la escribe, el orquestador la actualiza
  tras cada ronda y cambio de head, y quien edita el cuerpo la conserva.
  Esta línea sustituye la exclusión de veredictos «CLEAN» del cuerpo en
  «Opening a PR». En una review con [Consejo](references/consejo.md), cada
  miembro lleva su línea, y la tabla del consejo va antes de esas líneas.

## Merge

Al abrir el PR de tu unidad, continúa hasta mergearlo con el
[procedimiento de merge](references/merge.md), sin esperar otro pedido.
También se aplica al acumulador y a los PR de la cola de tu programa.
Mergea con APTO y verde sobre el head que aterriza, y verificado si el
merge despliega o el cambio es de alto riesgo. La autoridad cubre
los efectos que la automatización del merge dispara por sí sola, como
despliegues, migraciones, borrados y escrituras en sistemas de cliente.
Ejecutarlos a mano sigue las pausas de **Autonomía**. El procedimiento
fija esas condiciones y los PR que se detienen en merge-ready, como los
que tocan CI, dependencias o la gobernanza de agentes.

## Autonomía

Manda la autorización vigente del contrato. Decide el enfoque y recorta el
trabajo de implementación dentro del objetivo autorizado siguiendo los
principios y playbooks de pstack. Si un playbook pide al operador elegir
entre otra ronda o un alcance menor tras intentos fallidos, como
Autopilot-full 4, decide tú dentro del límite de **Rondas**. Registra la
evidencia y la decisión, informa y continúa sin esperar autorización.
Conserva los requisitos explícitos y la condición de terminado, las reviews
y los gates, y las demás pausas.

Al cambiar de enfoque, recortar el trabajo o dividir el cambio, incluido
elegir un alcance menor:

- En alto riesgo, el enfoque nuevo pasa por **Diseño disputado** antes de
  implementarse.
- El brief de cada ronda siguiente lleva el brief original y su condición
  de terminado sin reescribir, además de tu decisión registrada. El
  revisor juzga contra el original.
- El conteo de rondas sigue. Cada parte de una división es una unidad de
  **Unidades** o entra al acumulador.

Fuera de la autoridad de **Merge** y de lo ya autorizado por el usuario,
además de «Always pause» de pstack, pausa antes de:

- hacer push directo a `main` o ejecutar un comando de deploy;
- escribir en ICG u otro sistema de un cliente en producción;
- actualizar el tracker más allá de lo que prescriben el flujo del
  repositorio y [Tickets de Linear](references/linear.md);
- instalar dependencias, cambiar CI o tocar la configuración global de
  agentes;
- implementar UI/UX sin un prototipo aprobado por el dueño, como pide el
  routing;
- la ronda 9 de review de un mismo cambio, como dice **Rondas**.

El resto del trabajo local y reversible avanza sin preguntar.

## Respuesta

Toda pregunta que necesite una respuesta, decisión o autorización de Felix
se hace con la herramienta de preguntas con opciones del entorno. Usa
`AskUserQuestion` en Claude Code; en Codex, `request_user_input_async` o
`request_user_input`, según disponibilidad y restricciones del modo actual.
Incluye el contexto necesario y dos o tres opciones claras, con la
recomendada primero. Conserva las autorizaciones vigentes y espera la
respuesta antes de actuar sobre una decisión pendiente. Si no hay una
herramienta de preguntas utilizable, explica esa limitación y presenta las
opciones numeradas en texto.

Responde en español con las reglas de «Writing the reply» de `poteto-mode`:
frases cortas, sin raya larga y cada afirmación con su evidencia o su
etiqueta (medido, inferido, suposición). Nombra los principios que cambiaron
una decisión.
