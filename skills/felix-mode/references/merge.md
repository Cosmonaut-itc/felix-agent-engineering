# Mergear un PR APTO y verde

Este procedimiento da autoridad de merge para los PR de tu unidad y los de
la cola de tu programa, incluido el acumulador. Un pedido de consultar
estado o atender comentarios conserva su alcance. Un PR ajeno, fuera de la
cola o reservado por el usuario sigue esperando autorización explícita.
Una restricción del usuario o del `AGENTS.md` conserva su precedencia.

## Llevar el PR hasta verde

Después de abrir el PR, sigue Babysit hasta merge-ready, también si sólo
cambia documentación. Esto sustituye su entrada sólo por pedido y su modo
`check` para PR pequeños. En un programa, el trabajador devuelve el PR y
sus evidencias al orquestador. No mergea ni queda vigilándolo.

En T3 Code, usa `watch_pull_request` para esperar checks y eventos, y
termina el turno. Al despertar, vuelve a comprobar los gates antes del
merge. El watch programa la continuación. Fuera de T3, usa el mecanismo
del playbook Babysit. No mantengas dos watchers del mismo PR.

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

Ese APTO y su evidencia cumplen el veredicto independiente de Shipping 1.
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
   review y las pruebas del paso 2. Ante duda, renueva ambos.
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
   Un revert también necesita APTO y verde. Ejecutar un rollback manual
   conserva las pausas de **Autonomía**.

En un programa mergea el orquestador o su único stacker. El brief del
stacker incluye este archivo, el alcance autorizado, el destino, el head y
las evidencias APTO y verdes. Si alguna cambió o falta, devuelve el bloqueo.
Registra cada merge en `decisions.tsv`. En T3, enlaza cada PR con
`link_pull_request` y retira el watch al entregar el resultado.

La comprobación de base es una observación, no un bloqueo entre sesiones.
`--match-head-commit` protege el head, no la base. Sigue existiendo una
carrera si otra sesión mergea o retargetea entre la lectura y la petición.
