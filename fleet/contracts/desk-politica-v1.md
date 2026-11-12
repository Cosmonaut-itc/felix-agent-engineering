# desk-politica-v1 — retirado el 2026-11-12

El desk toma turno con [Pueue](../pueue-desk/). Se retira el contrato de
admisión, los carriles, claims, mutex, writer y armado nocturno de deskpolicy.
Los lanzadores de cada repositorio encolan en su grupo de Pueue y el desk
decide las cuotas de día y noche. Este documento ya no autoriza admisión.

[El retiro](../deskpolicy/) instala una respuesta terminal en la ruta de la
CLI anterior. Cualquier operación imprime la instrucción de actualizar la
rama a `origin/main` y usar el lanzador del repositorio, y termina con 64.
No genera recibos ni solicitudes nuevas. `pao-wait/`, recibos y demás estado
anterior permanecen como registro; no se borran ni se reutilizan para admitir.

El cambio se integra y despliega sólo durante el corte coordinado, después
de migrar Hasito, Jale y apotheca a Pueue y drenar el trabajo anterior.
