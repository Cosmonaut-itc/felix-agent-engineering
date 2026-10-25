# Implementar tickets acoplados en un PR acumulador

Usa este flujo cuando una spec contiene tickets que necesitan salir juntos
por un contrato, schema o migración compartidos. Conserva fuera los tickets
que pueden llegar solos a producción. No agrupes sólo por tener muchos
tickets. El programa entrega un PR acumulador hacia la rama de entrega,
normalmente `main`, desde una rama de integración.

Lee `implement-spec` de Matt Pocock. Conserva su grafo de dependencias,
frontera de tickets disponibles y worktree por implementador. Aplica los
pasos siguientes sobre sus pasos 3 a 9. Sustituyen su draft PR, su revisión
final con `code-review`, su TDD y sus merges de trabajadores. Una adaptación
local exigida por `AGENTS.md` mantiene sus controles y su precedencia.

## Preparar el programa

1. Crea el store de **Programas**. Guarda en las standing orders la spec,
   la rama de integración, su worktree, la rama de entrega y el único
   propietario de la integración. Usa Orchestrate para el programa largo
   y el playbook Feature de pstack para cada ticket, con `tdd` de pstack,
   **Verificación**, routing y **Revisión** de felix-mode.
2. Registra cada ticket en `units.tsv`, sin inventar un PR por ticket. En
   este flujo sus filas representan partes de la unidad de entrega. Guarda
   la relación con el PR acumulador en `overview.md` y sus decisiones en
   `decisions.tsv`. Un ticket integrado aún no equivale a desplegado. Sigue
   el tracker del repositorio para cerrarlo, sin anticipar la entrega.
3. Crea la integración desde la rama de entrega actual. Abre el acumulador
   como PR normal después de integrar el primer ticket. Su descripción
   enumera lo integrado, lo pendiente y la condición de terminado. Marca
   explícitamente que está en curso. Su elegibilidad para merge queda
   cerrada hasta completar la spec, la review final y los gates. La apertura
   temprana sólo sirve para seguir el trabajo y ejecutar CI. No actives
   auto-merge del forge.

## Implementar e integrar

4. Antes de cada despacho, fija el SHA de integración del trabajador y sus
   rutas de escritura en el brief. Prepara su worktree desde ese SHA.
   Los tickets bloqueados esperan a que sus dependencias estén integradas
   y verdes. Los tickets con rutas o contratos compartidos se secuencian,
   o reciben primero un contrato común verificado. No despaches dos
   propietarios simultáneos de la misma ruta.
5. Cada implementador escribe sólo en su rama y devuelve el SHA candidato,
   la base recibida y las pruebas. No resetea ni rebasa un checkout con
   trabajo, no incorpora por su cuenta la punta cambiante de integración,
   no escribe en integración y no abre ni mergea PRs. Esto sustituye esos
   pasos de `implement-spec`. El orquestador veta la entrega y obtiene la
   **Revisión** independiente de ese ticket sobre su candidato fijo.
6. El único integrador incorpora un ticket a la vez. Si integración sigue
   en la base verificada, incorpora el SHA aprobado sin modificarlo. Si
   avanzó, prepara en una rama de candidato el resultado de combinar la
   integración actual con el ticket. Verifica ese resultado y consigue
   APTO para su SHA, con todos los revisores del nivel vigente. Una review
   delta usa las reviews previas y examina conflictos e interacciones.
   Toda corrección de código o conflicto se delega. El integrador publica
   sólo el candidato APTO y verde, sin reescribir integración.
7. Avanza integración sólo si todavía apunta al SHA usado para preparar el
   candidato. Si cambió, reconcilia y repite el paso 6. Publica con push
   normal y comprueba el SHA remoto. Un rechazo de fast-forward no permite
   forzar. El coordinador y el stacker no escriben a la vez en integración.
   Guarda base, candidato, pruebas y reviews por ticket en el store, y
   actualiza el acumulador y la frontera después de cada integración.

La frontera de tickets sale del grafo de la spec y de `units.tsv`.
`orch frontier set` describe cadenas de PRs, no tickets sin PR. Ejecútalo
cuando exista el acumulador, para su cadena de un PR, sin simular eslabones
por ticket. Las evidencias por ticket viven en sus informes del store.
El ledger por PR y head se usa para la verificación final del acumulador.

## Cerrar la entrega

8. Cuando todos los tickets estén integrados, incorpora la rama de entrega
   actual a un candidato final. El integrador hace la sincronización y
   delega los conflictos. Ejecuta los gates del conjunto y sus pruebas de
   integración sobre ese candidato. Publica sin reescribir integración.
   Ningún trabajador pendiente puede modificar el candidato que se revisa.
9. Obtén la **Revisión** final del acumulador sobre ese head y su base. Da
   al panel el diff completo, la spec y las evidencias por ticket. Centra
   el trabajo nuevo en composición, contratos compartidos, conflictos,
   criterios de la spec y cambios que aún no tengan review. Cada revisor
   emite APTO para el conjunto. Clasifica el riesgo del diff completo.
   Las correcciones repiten los gates y la review afectados, con el límite
   de **Rondas** aplicado al acumulador como cambio propio.
10. Con la spec completa, APTO final y verde, sigue [Merge](merge.md).
    El acumulador se mergea automáticamente, aunque dispare deploy. Aplica
    también la comprobación de la base contra cambios de otras sesiones.
    Confirma merge y entrega antes de cerrar tickets que dependan de ella.
    Limpia sólo los worktrees propios cuyo trabajo y evidencias quedaron
    conservados y cuyos procesos terminaron.

Este flujo concentra la integración del programa en un escritor. Otra
sesión puede seguir cambiando `main`: sincroniza durante el programa cuando
su avance afecte a los tickets, además de la sincronización final. Ante
colisiones repetidas, secuencia las áreas compartidas con esa sesión.
