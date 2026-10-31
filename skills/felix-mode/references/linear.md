# Tickets de Linear

En Linear, cada ticket muestra qué sesión lo trabaja. La sesión es el hilo de
T3 que lo toma. En un programa, es el hilo del orquestador.

1. **Etiqueta.** El equipo SEN tiene el grupo de etiquetas `sesión`, de
   selección única. Tu sesión usa una etiqueta de ese grupo con el slug de su
   store, o un slug corto del hilo si no tiene store. Si no existe, créala con
   `save_issue_label` y `parent: sesión`.
2. **Título.** Renombra el hilo con `t3_thread_update` (`action: rename`) y
   el tema general de lo que implementa, en palabras que Felix entienda sin
   abrir Linear, como `Precios nuevos y correo de acceso de Jale`. El título
   expresa el tema general y omite los IDs de tickets, porque la etiqueta
   ya liga los tickets con la sesión.
   Cámbialo sólo si cambia el tema.
3. **Tomar.** Lee el ticket con `get_issue`. Está libre si no lleva etiqueta
   `sesión` o si la que lleva está retirada. Si lleva otra etiqueta activa,
   déjalo y avisa al usuario, que decide si la sesión anterior murió. Si está
   libre, en un solo `save_issue` añade tu etiqueta con `addLabels` y pasa el
   estado a In Progress. Un ticket ya terminado (In Review, Done o Canceled)
   conserva su estado y sólo recibe la etiqueta. Comenta:
   `Tomado por la sesión <slug>: hilo «<título>», rama <rama>, worktree <ruta>, store <ruta o «sin store»>.`
4. **Comprobar.** Linear no tiene escritura condicional. Si dos sesiones
   toman el mismo ticket, gana la última etiqueta. Vuelve a leer el ticket
   después de tomarlo y antes de cada lanzamiento, ronda, commit, push o
   merge sobre él. Si la etiqueta ya no es la tuya, detén ese trabajo,
   comenta lo que dejaste y avisa al usuario.
5. **Soltar.** Si dejas un ticket sin terminar, quita tu etiqueta con
   `removeLabels` y comenta `Soltado por la sesión <slug>: <motivo y estado>.`
   Un ticket terminado conserva la etiqueta, porque tu sesión atiende su
   review.
6. **Traspasar.** Si otro hilo retoma el programa, como en Session pickup,
   la etiqueta sigue siendo la del store. El hilo nuevo primero pone su
   título como en el paso 2 y después comenta
   `Retomado por el hilo «<título>»` en cada ticket abierto del programa.
7. **Cerrar.** Al cerrar la sesión o el programa, retira su etiqueta con
   `retire_issue_label`. Sigue visible en los tickets, nadie la reusa y sus
   tickets quedan libres si se reabren.
