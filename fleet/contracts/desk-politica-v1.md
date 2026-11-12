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

El corte debe anunciarse a las sesiones con esa instrucción. Los clientes
viejos no la muestran en su terminal: Hasito imprime
`No se pudo consultar politica estado: código 64` y guarda la instrucción en
`politica-1.stderr` y `lanzador.log` de la evidencia. Jale transforma el 64 en
`EXIT=75` en aproximadamente 1 s; la instrucción está en el log de la corrida,
que también dice `no free slot after 90 min` aunque no haya esperado ese plazo.
PAO no puede cambiar estas salidas de los clientes viejos.

El retiro instala primero el rechazo. Si quedan corridas `ejecutar`, conserva
writer y unidades, no envía señales y sale con 2 indicando cuántas quedan;
`--esperar N` permite esperar hasta N segundos. Se repite al terminar el trabajo
admitido. Sólo con el desk drenado se detienen writer y unidades. Un fallo
posterior conserva el rechazo instalado y requiere repetir el retiro.
