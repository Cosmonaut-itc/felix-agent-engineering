# Mergear un PR APTO y verde

En un repo no inscrito, o después de entregar en sombra, sigue el
procedimiento de merge de este archivo que autoriza **Merge** de felix-mode.
Estos PR se detienen en merge-ready y esperan autorización explícita:

- un PR ajeno, fuera de la cola o reservado por el usuario;
- un PR cuyo diff toca CI, manifiestos o lockfiles de dependencias, o la
  gobernanza de agentes, como instrucciones, contrato, routing, skills,
  hooks, configuración o instaladores de agentes, incluidos `AGENTS.md` y
  `CLAUDE.md`.

Un pedido de consultar estado o atender comentarios conserva su alcance.
Una restricción del usuario o del `AGENTS.md` conserva su precedencia.

## Entrega al tren

Consulta la inscripción en
`${XDG_CONFIG_HOME:-$HOME/.config}/pao-tren/inscritos`: una línea
`owner/repo` (activo) o `owner/repo sombra`, comparación sin distinguir
mayúsculas y comentarios `#`. Un modo desconocido es activo; entre duplicados
prevalece activo. Sin archivo, sigue el procedimiento habitual.
En modo activo esta sección sustituye el merge del ticket, también en un
programa: el tren es el único camino a `main`. En sombra cumple los pasos
1 y 3 y después continúa en «Llevar el PR hasta verde» con los gates y las
autorizaciones habituales; la suite completa sigue a cargo del ticket.
Anota `Retenido` o `Rechazado` y su motivo en tu registro sin frenar el merge.
En sombra no edites `heads_autorizados`.

1. Consigue APTO literal sobre el head actual, cero hallazgos abiertos,
   recibo limpio de la dirigida y CI verde. La base del PR es `main`.
   En alto riesgo exige también **Verificado**. La suite completa la corre
   el tren sobre C, el commit candidato del lote, sólo en modo activo.
2. En modo activo, un PR de alto riesgo, o uno que toca un patrón de
   `sensibles` del repo en la configuración del tren (en Jale: CI,
   despliegue, gobierno y lockfiles), queda en merge-ready hasta que su head
   esté en `heads_autorizados`. En Jale no le
   pidas el OK a Felix: lo autorizó el 2026-11-27 por evidencia. El APTO de
   la entrega debe ser un archivo local con ruta absoluta, no una URL de
   comentario, y traer `verificado` y una lista `revisores`. Esa lista lleva
   el veredicto de Sol y el de Opus, los dos en `high` y sobre el mismo head:

   ```json
   {"veredicto":"APTO","head":"<SHA40>","hallazgos_abiertos":0,"verificado":"Verificado",
    "revisores":[{"modelo":"gpt-6.2-sol","esfuerzo":"high","veredicto":"APTO","head":"<SHA40>"},
                 {"modelo":"opus-5.5","esfuerzo":"high","veredicto":"APTO","head":"<SHA40>"}]}
   ```

   Antes de entregar uno de esos PRs en Jale, mira el APTO que tienes. Si
   no trae los dos veredictos sobre el SHA que vas a entregar, pide el que
   falte sobre ese SHA, como fija el **Par de Jale** de `SKILL.md`. Pasa así
   si la última ronda tuvo un solo revisor, o si el head cambió después,
   también por una sincronización con `APTO vigente`. Decide si el PR es
   uno de esos por su diff, contra esos patrones y la clasificación de
   riesgo, no por el motivo con que el tren retiene el head. Un NO APTO del
   par sigue **Rondas** como cualquier otro, también su pausa. La vía manual
   de abajo sirve sólo si no puedes conseguir a uno de los dos revisores.
   `local.tren.ventanas-auto` añade el head a `heads_autorizados` en unos
   2 min y deja la procedencia en su `state.json`. El tren sigue exigiendo
   CI verde y, con migración, el journal sincronizado con `main`. No edites
   `heads_autorizados` a mano. En otro repo activo, o sin `revisores`, el
   head espera a que Felix lo autorice con AskUserQuestion. Con su OK, añade
   el SHA40 a `repos["<owner/repo>"].heads_autorizados` en
   `${XDG_STATE_HOME:-~/.local/state}/pao-tren/config.json` y conserva las
   demás claves. Anota el head y la cita de la decisión en tu registro.
   Después entrega. El PR viaja solo salvo que `sensibles_en_lote` del repo
   incluya todas sus categorías sensibles y, si es de alto riesgo, el repo
   tenga `alto_riesgo_en_lote`. Un head con ventana de migración siempre
   viaja solo. La autorización del head no cambia. Los demás límites de
   autorización del archivo siguen vigentes.
3. Registra `watch_pull_request` antes de entregar. Ejecuta:

   ```sh
   pao-tren entregar --repo <owner/repo> --pr <n> --head <SHA40> \
     --apto <ruta-del-veredicto-o-URL-del-comentario> --dirigida <ruta-del-recibo>
   ```

   El veredicto y el recibo son JSON sin texto alrededor, en un archivo o
   en el cuerpo de un comentario del mismo PR:

   ```json
   {"veredicto":"APTO","head":"<SHA40>","hallazgos_abiertos":0}
   {"head":"<SHA40>","resultado":"limpio"}
   ```

   En alto riesgo el veredicto añade `"verificado":"Verificado"`. Un formato
   distinto deja la entrega retenida.
   Guarda el recibo `estado=custodia`. Después ejecuta:

   ```sh
   pao-tren tick --repo <owner/repo> --seco
   pao-tren estado --repo <owner/repo>
   ```

   Comprueba la admisión de tu PR y head en sombra, separada de la custodia.
   Un head autorizado por APTO doble puede seguir `Retenido` en `--seco`,
   porque la simulación no lee las autorizaciones como el tick activo.
   Compruébalo en `estado` después de que pasen el autorizador y un tick
   activo, y mira `ventanas-auto.log` si no aparece.
   En modo activo, si quedó `Retenido` o `Rechazado`, resuelve el motivo
   o infórmaselo a Felix antes de terminar el turno. Si la entrega falla,
   resuelve su motivo o informa el bloqueo. Termina con el watch registrado.
4. Ante un comentario de rojo del tren, lee el lote, B, C, unidad, firma,
   conjunto causal y comando de reproducción. Corrige con prueba dirigida,
   pasa la ronda de revisión correspondiente y entrega el head nuevo con
   su APTO y recibo nuevos. Renueva la autorización si corresponde: el APTO
   doble del head nuevo o el OK de Felix.

El tick de launchd promueve C probado con push CAS fast-forward y comprueba
`main == C` y su árbol. Las sesiones de Claude no mergean ni hacen push a
`main` del repo inscrito activo; `merge-guard` lo niega sin excepción para el tick.
En sombra el tren sólo observa y el guard conserva las reglas habituales de merge.

## Sincronizar una vez, al final

Cada push a un PR corre todos sus checks, y cada relanzamiento completo los
corre de nuevo. Donde el CI tiene cupo, ese gasto deja sin checks a las
demás sesiones: en Jale, el 40 % de los arranques de CI de un día corrió
sobre commits que sólo mezclaban `main` (medido el 2026-11-16).

- **Una sincronización.** La rama del PR incorpora la rama destino en el
  paso 2 de «Aterrizar el head aprobado», con APTO y verde ya sobre su
  head. Hasta entonces mide la deriva sin empujar: después de `git fetch`,
  `git merge-tree --write-tree --name-only <destino> HEAD` sale con 1 si
  hay conflicto y lista sus archivos. Un aviso de otra sesión sobre un
  cambio en el destino se toma en esa sincronización final. En un
  [acumulador](acumulador.md), la sincronización final es su paso 8: el
  candidato final incorpora la rama de entrega antes de los gates y de la
  revisión del conjunto.
- **Sincronización anticipada.** Una capa de una pila sincroniza antes
  sólo por los motivos de
  [Pilas](#pilas-la-base-aterriza-primero). Los demás PR sincronizan antes
  sólo por uno de estos motivos, y lo anotas en tu registro:
  - el PR tiene conflicto y necesitas su CI, que el forge no corre sobre un
    PR en conflicto;
  - la rama necesita un cambio que ya está en el destino: un arreglo del
    que depende tu prueba, o la infraestructura de pruebas que un lanzador
    exige al fallar con un código de admisión;
  - `scripts/risk-classify` lo pide, como dice **Riesgo**.

  Esa sincronización viaja en el mismo push que las correcciones
  pendientes.
- **Relanzar lo fallido.** Para repetir un check, relanza sólo los jobs que
  fallaron: `gh run rerun <run-id> --failed`, o `--job <job-id>` para uno.
  Antes confirma que el fallo no viene del cambio y, si el CI tiene cupo,
  que queda cupo, como digan los docs del repositorio.

## Pilas: la base aterriza primero

Una pila es un PR cuya rama base es la rama de otro PR abierto. Ese otro PR
es la **base** y el PR que nace de ella es una **capa**. Cuando una capa
sigue cada movimiento de su base, paga una ronda de sincronización y una
corrida de CI por cada uno: en Jale, las sincronizaciones del PR 660
dispararon rondas en sus cuatro capas durante 6 días (medido el
2026-11-17). Esta regla sustituye el rebase de cada capa sobre la punta de
su base que pide «Size and stacks» de pstack.

- **La base primero.** La base recibe antes que sus capas la revisión, la
  verificación y el merge, con las reglas de este archivo.
- **La capa quieta.** Mientras su base siga abierta, la capa conserva el
  head de la base del que nació, su **head fijo**. Sus pushes llevan sólo
  trabajo propio y sus rondas revisan su diff contra el head fijo. Un commit nuevo en la base,
  una sincronización de la base con el destino, el aviso del forge de que
  la rama está desactualizada y el aviso de otra sesión se toman en la
  sincronización única de la capa.
- **Riesgo de la capa.** Antes de cada ronda, la capa corre
  `scripts/risk-classify <head fijo>`. Con ese destino
  el script clasifica sólo el diff de la capa y su resultado es exacto, así
  que nunca pide actualizar la rama.
- **Conflicto real.** La capa incorpora su base antes sólo por uno de estos
  motivos, y lo anotas en tu registro:
  - después de `git fetch`,
    `git merge-tree --write-tree --name-only <head de la base> HEAD` sale
    con 1, y la capa necesita su CI o seguir editando los archivos que
    lista;
  - una corrección de la base cambió un contrato que la capa usa, como una
    firma, un schema o una migración, y la capa mezclada con el head nuevo
    de la base ya no compila o falla su prueba dirigida.

  Esa incorporación viaja en el mismo push que el trabajo pendiente de la
  capa, y el head incorporado es el nuevo head fijo. Su ronda la fija
  `scripts/apto-vigente`, con la rama de la base como rama destino.
- **Una sincronización por capa.** Cuando la base aterriza, la capa que
  nació de ella apunta su PR al destino e incorpora el destino una vez. Si
  ya tenía APTO, `scripts/apto-vigente` decide su ronda, como en el paso 3
  de «Aterrizar el head aprobado». Esa capa es ahora la base de las que
  tiene encima.
- **Base cerrada sin merge.** Sus capas se detienen: conservan su rama y su
  rama base, y esperan sin sincronizarse ni entregarse. Avisa a quien pidió
  el trabajo y sigue con la salida que elija. Si otro PR reemplaza a la
  base, la capa lo incorpora una vez como base nueva. Si la capa absorbe el
  cambio de la base, apunta su PR al destino y su siguiente ronda es
  completa, porque su diff creció. Si el trabajo se abandona, la capa se
  cierra.
- **Con tren.** El tren exige que la base de cada PR sea el destino, así que
  una capa se entrega al tren después de su sincronización única.

## Llevar el PR hasta verde

Después de abrir el PR, sigue Babysit hasta merge-ready, también si sólo
cambia documentación. Esto sustituye su entrada sólo por pedido y su modo
`check` para PR pequeños. En un programa, el trabajador devuelve el PR y
sus evidencias al orquestador y ahí termina su encargo. El merge y la
vigilancia son del orquestador o de su único stacker.

Un acumulador en curso observa CI y comentarios en `background` o `check`,
y busca merge-ready sólo con la spec completa. Cualquier
corrección entra por el candidato del paso 6 de [Acumulador](acumulador.md),
con su único integrador. Después del candidato final sigue el flujo normal.

En T3 Code, comprueba los gates antes de ceder la espera y en cada despertar.
En GitHub, usa el ejecutable de Babysit 6,
`<plugin pstack>/skills/poteto-mode/scripts/watch-pr/watch-pr`, con
`--owner <owner> --repo <repo> --pr <n> --interval 20 --timeout 150`, sin
`--status-only`. El plazo permite la confirmación de 60 segundos sin checks.
Ejecuta la comprobación en background o con un plazo de herramienta de al
menos 180 segundos y recoge su terminal. `WAITING` es una salida intermedia.
Sólo `READY` para ese PR y el head aprobado confirma merge-ready.
Ante `BLOCKER`, sigue Babysit. Si termina en `TIMEOUT` por `pending-checks`,
informa qué checks faltan, conserva o rearma `watch_pull_request` de T3 y
termina el turno. Ante otro `TIMEOUT` o un fallo de lectura, informa la
razón y retira el watch para devolver el hilo al inbox, porque un nuevo
evento no está garantizado. Reserva `--status-only` para observar un
acumulador en curso. Su salida no confirma merge-ready.
En Origin, usa las lecturas y el criterio de merge-ready de Babysit,
incluidos hilos y reviews bloqueantes. T3 es el único watcher persistente.
Fuera de T3, usa el mecanismo del playbook Babysit.

Estas condiciones deben cumplirse juntas:

- La unidad cumple su condición de terminado. En un acumulador, todos los
  tickets están integrados y existe la review final del conjunto.
- **APTO.** La última ronda de **Revisión** dio APTO para el head actual y
  la base evaluada, o ese APTO sigue vigente por el paso 3. En una ronda
  completa lo dan todos los revisores del nivel, que en alto riesgo son A y
  B. En una ronda delta lo da su revisor. El veto del orquestador deja cero
  critical y cero warning abiertos. Un APTO condicionado a cambios no basta.
- **Verde.** La **Verificación** y los gates del repositorio pasaron sobre
  ese head. Todos los checks exigidos terminaron con éxito, sin pendientes
  ni gates omitidos. Babysit confirma merge-ready en el forge activo, sin
  conflictos ni hallazgos reales pendientes. Cero checks configurados no
  sustituye la verificación del repositorio.
- **Verificado.** Si el merge despliega, de inmediato o por una
  automatización programada que identifica el paso 1, o el cambio es de
  alto riesgo, un verificador que no escribió el código corre el cambio sobre
  ese head como pide Shipping 1 y da `PASS` o `PASS+NOTES`. Usa el modelo
  de `swarm workers` del routing.

Un hallazgo real abierto impide merge-ready. Si lo llevas al dueño, sitúa
el defecto respecto de la rama destino: di si ya está en ella o si este
merge lo introduce. Llama «preexistente» sólo a un defecto que la rama
destino ya tiene.

Sin deploy ni alto riesgo, ese APTO y su evidencia cumplen el veredicto
independiente de Shipping 1. En los demás casos lo cumple **Verificado**.
La autoridad sustituye la espera del operador en Babysit 9, Shipping,
Autopilot-full 5 y Autopilot-stack, incluido su permiso para corregir un
problema durante el aterrizaje. Conserva los límites de alcance, las pausas
restantes y la separación entre propietario, verificador y stacker.

## Aterrizar el head aprobado

1. **Fijar el destino.** Resuelve el forge como Shipping 1. Guarda repo, PR,
   head, rama base y SHA base con la evidencia. El destino es la rama de
   entrega del repositorio, normalmente `main`. Identifica qué automatización
   dispara y cómo observarla en los docs del repositorio.
2. **Actualizar la evidencia.** Trae esa rama justo antes del merge. Si
   avanzó, aplica la prueba de Autopilot-full 5 contra esa rama, incluyendo
   conflictos, archivos compartidos, cambios de CI, lockfiles y dependencias.
   Si hace falta rebase, lo ejecuta el propietario de la topología, fuera
   de Babysit. Si exige un tercer rebase desde merge prep, detén ese PR y
   avisa para secuenciar el trabajo que colisiona.
3. **Renovar APTO.** Un head nuevo o una rama destino que avanzó exigen
   checks nuevos. Para saber si el APTO sigue vigente, corre desde el
   checkout `scripts/apto-vigente <rama destino> <head revisado> <head nuevo>`
   de esta skill y sigue su primera línea:
   - `APTO vigente`: el head nuevo es sólo la mezcla sin conflictos del head
     revisado con la rama destino, y esa rama no cambió archivos del parche.
     También lo conserva si los únicos archivos compartidos son catálogos
     `messages/*.json` o `translations/*.json` (en cualquier directorio),
     objetos anidados de cadenas: ambos lados sólo agregan claves disjuntas
     y el catálogo nuevo es exactamente su unión. La segunda línea confirma
     esa excepción. Valores cambiados, claves borradas o duplicadas, JSON
     inválido y conflictos exigen revisión. El APTO sigue vigente sin ronda
     nueva; guarda la salida con la prueba del paso 2.
   - `Ronda delta`: la rama destino cambió los archivos del parche que
     lista, o la mezcla tuvo conflictos. Con conflictos, la segunda línea
     es el diff de su resolución y la lista incluye los archivos en
     conflicto. Corre una ronda delta sobre esos archivos y ese diff. Es una
     ronda de sincronización.
   - `Ronda`, o un error del script: corre la ronda que fija **Rondas**.

   Una ronda de sincronización no cuenta para el límite de **Rondas**. Las
   demás rondas sí cuentan. Un veredicto de
   **Verificado** sobrevive a un head nuevo sólo según Shipping 3.
4. **Merge protegido.** Sigue Shipping 4 y su referencia `merge-safety.md`,
   con inspección y cancelación de solicitudes pendientes antes de cambiar
   topología. Mergea un PR a la vez, de abajo arriba. En GitHub usa
   `gh pr merge <pr> --repo <owner/repo> --squash --match-head-commit <head aprobado>`.
   Mergea siempre con ese comando, en una llamada propia y con valores
   literales, porque en Claude Code el hook `scripts/merge-guard` lo revisa.
   El hook niega el merge sin `--match-head-commit`, con un flag que no
   admite, como `--admin` o `--auto`, o cuando no puede analizar el comando.
   Si lo niega, repite el comando con la forma que indica su razón. El hook
   también pide la confirmación del dueño cuando el diff toca CI,
   dependencias o gobernanza de agentes, o cuando no puede comprobar los
   archivos del PR. Para buscar o citar ese comando en otra
   llamada, escríbelo como `gh pr merg[e]` o pásalo en un archivo.
   Confirma la rama destino inmediatamente antes de ejecutarlo. No uses
   `--auto` ni `--admin`, ni cambies las protecciones. En otro forge sin
   condición equivalente del servidor sobre el head, para en merge-ready.
   Si la rama obliga a usar cola, informa esa limitación y detente sin
   admitir el PR a la cola. No dejes una solicitud que aterrice otro head.
5. **Confirmar.** Relee el PR, exige estado merged y la base esperada, trae
   el destino y confirma que contiene el merge commit. Una respuesta de
   admisión a cola no confirma merge. Si hay un eslabón siguiente, sigue el
   retarget y replay de Shipping 4 y renueva sus gates como en el paso 3.
   Si el estado contradice lo esperado, detén la cadena y reconcilia.
6. **Comprobar después.** Sigue el CI del destino y el deploy observable.
   Si un despliegue programado o externo no puede comprobarse en la sesión,
   informa que está pendiente, con su cadencia o enlace si los conoces.
   No lo declares verificado. Ante CI rojo o fallo de deploy, avisa de
   inmediato y abre la unidad de corrección prioritaria de **Unidades**.
   Un revert cumple las mismas condiciones. Ejecutar un rollback manual
   conserva las pausas de **Autonomía**.

En un programa mergea el orquestador o su único stacker. El brief del
stacker incluye este archivo, el alcance autorizado, el destino, el head y
las evidencias de cada condición. Si alguna cambió o falta, devuelve el
bloqueo.
Registra cada merge en `decisions.tsv`. En T3, enlaza cada PR con
`link_pull_request` y retira el watch al entregar el resultado.

La comprobación de base es una observación, no un bloqueo entre sesiones.
`--match-head-commit` protege el head, no la base. Sigue existiendo una
carrera si otra sesión mergea o retargetea entre la lectura y la petición.
