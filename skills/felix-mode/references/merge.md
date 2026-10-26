# Mergear un PR APTO y verde

Sigue este procedimiento en cada merge que autoriza **Merge** de felix-mode.
Estos PR se detienen en merge-ready y esperan autorización explícita:

- un PR ajeno, fuera de la cola o reservado por el usuario;
- un PR cuyo diff toca CI, manifiestos o lockfiles de dependencias, o la
  gobernanza de agentes, como instrucciones, contrato, routing, skills,
  hooks, configuración o instaladores de agentes, incluidos `AGENTS.md` y
  `CLAUDE.md`.

Un pedido de consultar estado o atender comentarios conserva su alcance.
Una restricción del usuario o del `AGENTS.md` conserva su precedencia.

## Llevar el PR hasta verde

Después de abrir el PR, sigue Babysit hasta merge-ready, también si sólo
cambia documentación. Esto sustituye su entrada sólo por pedido y su modo
`check` para PR pequeños. En un programa, el trabajador devuelve el PR y
sus evidencias al orquestador. No mergea ni queda vigilándolo.

Un acumulador en curso observa CI y comentarios en `background` o `check`.
No intenta llegar a merge-ready hasta completar la spec. Cualquier
corrección entra por el candidato del paso 6 de [Acumulador](acumulador.md),
con su único integrador. Después del candidato final sigue el flujo normal.

En T3 Code, comprueba los gates antes de ceder la espera y en cada despertar.
En GitHub, usa el ejecutable de Babysit 6,
`<plugin pstack>/skills/poteto-mode/scripts/watch-pr/watch-pr`, con
`--owner <owner> --repo <repo> --pr <n> --interval 20 --timeout 150`, sin
`--status-only`. El plazo permite la confirmación de 60 segundos sin checks.
Ejecuta la comprobación en background o con un plazo de herramienta de al
menos 180 segundos y recoge su terminal: `WAITING` es una salida intermedia.
Sólo `READY` para ese PR y el head aprobado confirma merge-ready.
Ante `BLOCKER`, sigue Babysit. Si termina en `TIMEOUT` por `pending-checks`,
informa qué checks faltan, conserva o rearma `watch_pull_request` de T3 y
termina el turno. Ante otro `TIMEOUT` o un fallo de lectura, informa la
razón y retira el watch para devolver el hilo al inbox; un nuevo evento
no está garantizado. Reserva `--status-only` para observar un acumulador
en curso: su salida no confirma merge-ready.
En Origin, usa las lecturas y el criterio de merge-ready de Babysit,
incluidos hilos y reviews bloqueantes. T3 es el único watcher persistente.
Fuera de T3, usa el mecanismo del playbook Babysit.

Estas condiciones deben cumplirse juntas:

- La unidad cumple su condición de terminado. En un acumulador, todos los
  tickets están integrados y existe la review final del conjunto.
- **APTO.** Todos los revisores exigidos por **Revisión** dieron APTO para
  el head actual y la base evaluada. En alto riesgo son A y B. El veto del
  orquestador deja cero critical y cero warning abiertos. Los nits sin
  aplicar permiten APTO. Un APTO condicionado a cambios no basta.
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
3. **Renovar APTO.** Cualquier cambio de head o de rama destino exige
   checks y APTO nuevos. La review puede centrarse en el delta con la
   evidencia anterior, pero todos los revisores del nivel vigente reciben
   el diff completo y emiten su veredicto sobre el head nuevo. Cuenta como
   ronda del mismo cambio, con el límite de **Rondas**. Un patch-id idéntico
   puede conservar pruebas aplicables según Shipping 3, pero no sustituye
   ese APTO. Si sólo avanza el SHA base, documenta la aplicabilidad de la
   review y las pruebas del paso 2. Ante duda, renueva ambos. Un veredicto
   de **Verificado** sobrevive a un head nuevo sólo según Shipping 3.
4. **Merge protegido.** Sigue Shipping 4 y su referencia `merge-safety.md`,
   con inspección y cancelación de solicitudes pendientes antes de cambiar
   topología. Mergea un PR a la vez, de abajo arriba. En GitHub usa
   `gh pr merge <pr> --repo <owner/repo> --squash --match-head-commit <head aprobado>`.
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
