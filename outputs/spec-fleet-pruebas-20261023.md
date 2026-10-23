# Fleet: cola común de ejecución con prioridades y pausa de pruebas

Especificación para el proyecto `orchestation` de Linear, equipo SEN.
Felix confirmó el CLI público y las pruebas reales por plataforma como frontera
de verificación el 23 de octubre de 2026.

## Problem Statement

Felix reparte pruebas y otras cargas de desarrollo entre una Mac Mini y un
desktop Windows con WSL. Las reglas de selección, cola, aislamiento,
cancelación y vigilancia están repartidas entre proyectos, scripts instalados,
wrappers locales e instrucciones para los agentes.

Incorporar un proyecto, un agente o una computadora requiere conocer esas
convenciones y adaptar scripts. Una mejora no llega de forma uniforme a todos
los consumidores. Algunas protecciones están implementadas, otras viven en
ramas y otras siguen pendientes, por lo que no existe una garantía común de
admisión y recuperación de las corridas.

La cola actual tampoco permite gestionar de forma común la prioridad entre
proyectos ni ceder capacidad a una tarea urgente pausando pruebas compatibles.
Felix necesita que todos los agentes y proyectos entren casi sin configuración,
que las tareas importantes puedan adelantar pendientes y que los equipos
compartan capacidad sin invadir recursos o procesos de otras corridas.

## Solution

Crear Fleet como módulo de Felix Agent Engineering, con una interfaz pública,
protocolo y releases propios. Cualquier agente o proyecto envía trabajos al
mismo CLI. Fleet conserva una cola duradera, selecciona nodos compatibles,
coordina la reserva de recursos y devuelve estado, resultados y evidencia.

Un proyecto puede ejecutar un comando sin configuración adicional cuando sus
requisitos están disponibles. Para gates complejos declara perfiles con
etapas, recursos y política de pausa. Un nodo se incorpora mediante un registro
declarativo, comprobación de capacidades y una corrida de validación.

Las prioridades permiten adelantar pendientes con equidad entre proyectos.
Cuando falta capacidad, un trabajo urgente puede solicitar que un trabajo
compatible ceda al terminar una etapa segura, o que un intento explícitamente
reiniciable se cancele y vuelva a la cola. Fleet muestra cuándo se solicitó la
pausa, cuándo ocurrió y qué recursos quedaron disponibles.

Las mejoras se distribuyen mediante PAO con versiones inmutables y activación
gradual. Las corridas activas conservan su ejecutor y los proyectos mantienen
sus gates y reglas de validación.

## User Stories

1. Como Felix, quiero consultar todos los trabajos y nodos desde una interfaz común para conocer la capacidad del ambiente sin visitar cada proyecto.
2. Como agente de código, quiero enviar un comando desde el proyecto actual para ejecutar pruebas sin aprender un lanzador distinto por repositorio.
3. Como desarrollador de un proyecto nuevo, quiero usar valores conservadores sin crear configuración para integrar una prueba sencilla casi de inmediato.
4. Como responsable de un proyecto, quiero definir perfiles de ejecución para conservar sus gates, etapas, servicios y artefactos requeridos.
5. Como agente de cualquier proveedor, quiero consumir el mismo contrato y salida JSON para participar sin una integración específica del modelo.
6. Como agente, quiero recibir una identidad de trabajo al aceptarse el envío para seguirlo aunque la preparación todavía no termine.
7. Como agente, quiero repetir un envío con la misma clave de idempotencia para recuperar su identidad sin duplicar la carga.
8. Como agente, quiero consultar el estado después de perder la conexión para distinguir una desconexión de una corrida terminada.
9. Como responsable de una revisión, quiero conocer la instantánea exacta probada para ligar la evidencia con el código que reviso.
10. Como desarrollador, quiero poder probar cambios locales admitidos para validar un trabajo sin confundir su contenido con el SHA del último commit.
11. Como agente, quiero recuperar logs, resultado y artefactos por identidad para informar lo ocurrido con evidencia verificable.
12. Como agente, quiero que una ejecución sin resultado remoto acreditado aparezca como incompleta para evitar declarar éxito por un fallo del transporte.
13. Como Felix, quiero agregar una computadora por configuración declarativa para ampliar capacidad sin modificar los proyectos consumidores.
14. Como operador de un nodo, quiero comprobar sus herramientas y capacidades al incorporarlo para detectar requisitos faltantes antes de admitir trabajos.
15. Como responsable de un proyecto, quiero declarar requisitos de sistema y arquitectura para evitar asignaciones incompatibles.
16. Como Felix, quiero que Windows y WSL compartan el presupuesto de su equipo físico para impedir que se cuente dos veces la misma capacidad.
17. Como agente, quiero que el nodo confirme la reserva antes de empezar para evitar sobreasignaciones causadas por clientes simultáneos.
18. Como responsable de una suite, quiero checkouts, puertos y bases aisladas para ejecutar en paralelo sin contaminar resultados.
19. Como responsable de una suite con una BD compartida, quiero reservarla como recurso exclusivo para evitar colisiones con otra corrida.
20. Como Felix, quiero etiquetar trabajos por proyecto, suite o propósito para filtrar y entender la cola.
21. Como Felix, quiero asignar prioridades para que una tarea importante adelante trabajos pendientes menos urgentes.
22. Como responsable de un proyecto, quiero conservar un orden estable entre trabajos equivalentes para entender por qué uno empezó antes que otro.
23. Como Felix, quiero limitar quién puede elevar prioridades para que todos los agentes no conviertan su trabajo en urgente.
24. Como responsable de un proyecto, quiero que una espera prolongada mejore la prioridad efectiva para que el trabajo de fondo también reciba servicio.
25. Como Felix, quiero cuotas y límites a las ráfagas prioritarias para distribuir capacidad entre proyectos bajo carga sostenida.
26. Como agente, quiero conocer por qué mi trabajo espera para distinguir falta de capacidad, incompatibilidad, cuota, mantenimiento y estado dudoso.
27. Como Felix, quiero que una tarea urgente solicite capacidad a una prueba compatible para atenderla antes de que termine toda la suite.
28. Como responsable de una prueba, quiero declarar puntos seguros entre etapas para pausarla conservando avance acreditado.
29. Como agente, quiero distinguir pausa solicitada de pausa confirmada para no suponer que ya se liberó capacidad.
30. Como responsable de una prueba reiniciable, quiero autorizar la cancelación y repetición de su intento para ceder capacidad cuando no puede guardar avance.
31. Como responsable de QA interactiva, quiero proteger una sesión no interrumpible para que una prioridad ajena no termine mi trabajo.
32. Como agente, quiero reanudar un trabajo mediante la cola para que vuelva a reservar recursos con las mismas reglas que los demás.
33. Como responsable de una revisión, quiero que la reanudación conserve la instantánea y valide el entorno para no reutilizar evidencia de entradas distintas.
34. Como Felix, quiero que una reserva con heartbeat vencido quede identificada como dudosa para investigar sin arrancar una segunda copia por error.
35. Como operador, quiero recuperar el estado después de reiniciar el coordinador para conservar pendientes, asignaciones y resultados.
36. Como responsable de un nodo, quiero que los supervisores locales mantengan el control de sus cargas durante una caída del coordinador para que no queden sin dueño.
37. Como agente, quiero cancelar un trabajo propio y conocer cuándo concluyó su limpieza para liberar recursos con certeza.
38. Como Felix, quiero que la identificación de procesos incluya su contexto y nacimiento para evitar actuar sobre un PID reutilizado.
39. Como operador del desktop, quiero separar las identidades Windows y Unix para impedir que un registro de WSL autorice terminar un proceso Windows ajeno.
40. Como Felix, quiero consultar snapshots de salud sin lanzar diagnósticos costosos para que observar los equipos no agregue carga ni procesos huérfanos.
41. Como operador, quiero drenar un nodo y reabrirlo explícitamente para aplicar mantenimiento sin nuevas admisiones durante la ventana.
42. Como Felix, quiero que la prioridad respete mantenimiento, compatibilidad y safeguards para que una urgencia no desactive las protecciones del ambiente.
43. Como mantenedor, quiero distribuir una versión primero a un nodo de prueba para detectar fallos antes de activarla en los demás.
44. Como responsable de una corrida, quiero conservar la versión del ejecutor que la inició para que una actualización no cambie su comportamiento a mitad de ejecución.
45. Como operador, quiero ver las versiones deseadas y activas y poder volver a una versión compatible para recuperar el servicio ante una actualización defectuosa.
46. Como mantenedor, quiero migrar los lanzadores existentes sin colas que compitan para conservar exclusión mientras conviven clientes viejos y nuevos.
47. Como responsable de un proyecto, quiero conservar sus pruebas y evidencia durante la migración para cambiar el transporte sin debilitar su definición de terminado.
48. Como Felix, quiero que los cambios de estado y prioridad queden auditados para entender quién pidió una interrupción y qué ocurrió después.

## Implementation Decisions

- Fleet forma parte de Felix Agent Engineering como módulo separado. Su protocolo y releases tienen ciclo propio; los clientes dependen de la interfaz pública, no de archivos internos.
- El CLI público permite enviar, consultar, esperar, cancelar, pausar y reanudar trabajos; listar y comprobar nodos; drenar y reabrir nodos; e inspeccionar versiones. Devuelve JSON estable y códigos de salida documentados.
- Los perfiles de proyecto son opcionales. Un envío sin perfil usa prioridad normal, recursos conservadores y política no interrumpible. El nodo debe acreditar las herramientas requeridas antes de admitirlo.
- El contrato de trabajo incluye versión de esquema, identidad, clave de idempotencia, proyecto, emisor, instantánea, comando, requisitos, recursos, prioridad, etiquetas y política de interrupción. Reutilizar una clave con contenido diferente devuelve un conflicto explícito.
- El transporte conserva comandos como argv o guiones literales. La instantánea registra el SHA y una huella del contenido efectivo; los cambios locales admitidos forman parte de su identidad. Los secretos se resuelven por mecanismos privados y no se escriben en recibos o logs.
- Hay una cola duradera común y un coordinador inicial. El almacenamiento puede ser SQLite local al coordinador, accedido por su interfaz. No se comparte una base de datos por un sistema de archivos de red ni por sincronización de repositorios.
- SSH es el transporte inicial. El contrato deja espacio para adaptadores posteriores sin exigir un servicio nuevo por lenguaje o proveedor de agentes. La interfaz remota autentica al emisor y aplica sus permisos.
- El registro de nodos distingue equipo físico y ejecutores. SO, arquitectura, herramientas, dispositivos, límites y credenciales locales no se codifican como casos especiales por nombre de computadora.
- Cada equipo físico tiene un presupuesto común. Sus ejecutores confirman localmente una reserva antes del arranque. Una selección del coordinador sin confirmación no representa capacidad adquirida.
- La admisión reserva recursos compartidos entre proyectos, incluidas BD, puertos y dispositivos exclusivos. La capacidad es política del nodo y no puede ampliarse mediante una variable enviada por el cliente.
- Las asignaciones se ligan a trabajo, intento, nodo y generación de reserva. Una operación de un dueño anterior no puede liberar una reserva posterior. La repetición de confirmaciones y cierres converge al mismo estado.
- Las etiquetas describen y filtran. La prioridad es un campo validado con clases iniciales urgente, alta, normal y segundo plano. Las reglas que traduzcan etiquetas a prioridad pertenecen a una política común y explícita.
- El orden considera compatibilidad y recursos, prioridad efectiva y cuotas por proyecto. Conserva FIFO dentro de grupos equivalentes. La evidencia de estado explica qué regla bloquea o adelanta un trabajo.
- El envejecimiento de pendientes y los límites de servicio prioritario son configurables. La política limita ráfagas de prioridad alta o urgente y garantiza oportunidades de servicio a trabajos elegibles de menor prioridad. El envejecimiento por sí solo no otorga permisos de urgencia.
- Cambiar la prioridad de un trabajo requiere autorización del emisor o del operador según la política. Una solicitud inválida se rechaza con su motivo; no se convierte silenciosamente en otra prioridad.
- Adelantar pendientes no interrumpe trabajos activos. Para solicitar capacidad a uno activo se exige una política de interrupción declarada y verificada por el ejecutor.
- Las políticas iniciales son no interrumpible, pausa en punto seguro y cancelación con reinicio seguro. En la última, el perfil declara que repetir el intento es válido y define el cierre de sus efectos temporales.
- Una pausa comienza como solicitud. Sólo se confirma cuando termina la etapa segura, queda guardado el avance y el ejecutor informa los recursos liberados y los que todavía retiene. Una capacidad retenida nunca se contabiliza como libre.
- Reanudar devuelve el trabajo a admisión con su avance y política de prioridad. Conserva la instantánea y valida la compatibilidad del entorno. Si ya no puede acreditarse el avance, lo invalida explícitamente.
- La suspensión general mediante señales del SO no es el mecanismo de pausa. Un test largo sin soporte cooperativo puede continuar hasta terminar; sólo se cancela para repetirlo si declaró esa capacidad. QA interactiva y cargas con efectos externos son no interrumpibles por defecto.
- Los supervisores locales mantienen la identidad y control de sus cargas. El vencimiento de un heartbeat, una desconexión o un timeout no prueban que el proceso terminó. Una asignación dudosa no se duplica ni libera hasta verificar terminalidad o contención de la carga propia.
- El estado distingue aceptación, espera, preparación, ejecución, pausa solicitada, pausa confirmada, cancelación solicitada, finalización e incertidumbre. Los errores de transporte y la evidencia incompleta se conservan separados del resultado del comando.
- Las acciones sobre procesos verifican dueño, host, namespace, boot e identidad de creación. Windows y Unix tienen verificadores separados. El monitor observa; el launcher nativo sólo controla hijos que él mismo creó. La limpieza nativa general no se habilita por esta integración.
- La salud proviene de snapshots versionados con fecha y boot verificables. Dato desconocido no equivale a cero carga. Se preserva la distinción entre captura válida sin actividad, historial con tope y fallo de captura o persistencia.
- Observación y activación de un nodo son operaciones diferentes. Un nodo drenado no admite cargas y una consulta de salud no lo reactiva. La ejecución degradada requiere política visible y nunca ignora mantenimiento, incompatibilidad o identidad inválida.
- Los recibos conservan identidad de entradas, nodo, ejecutor, intentos, tiempos, cambios de estado, resultado y artefactos. Una interrupción parcial no se presenta como éxito del gate completo.
- La distribución usa instalaciones de versión inmutable. Cada corrida conserva su versión. La activación gradual comprueba compatibilidad de protocolo, drena admisiones y respeta corridas activas. El rollback incluye compatibilidad del estado persistido.
- Los lanzadores actuales se migran mediante adaptadores. Se preservan los mejores contratos existentes de instantánea, guion literal, aislamiento y evidencia. La transición garantiza una única autoridad para cada recurso y retira las copias editables del scheduler después de migrar sus consumidores.

## Testing Decisions

- Frontera principal propuesta: el contrato público del CLI. Las pruebas envían trabajos y observan estados, decisiones de admisión, procesos propios, resultados y artefactos. No dependen de tablas, funciones privadas ni nombres de archivos de estado.
- Una prueba es buena cuando detecta una conducta incorrecta visible para un proyecto, agente u operador. La implementación interna puede cambiar sin modificar sus expectativas. No se acepta que sólo se haya llamado una función o guardado un campo.
- El entorno de contrato ejecuta el CLI real con estado aislado, ejecutores de prueba y transporte controlado. Permite varios clientes y supervisores propios para verificar concurrencia y recuperación sin tocar corridas de otros proyectos.
- La misma familia de pruebas cubre incorporación de proyecto y nodo, idempotencia, reserva, prioridades, pausa, reanudación, cancelación, evidencia y actualización. Los escenarios usan reloj y señales de salud controlables desde los límites del entorno, sin esperar TTL reales ni medir carga artificial del host.
- Como precedente se reutiliza el enfoque de pruebas públicas del instalador PAO con directorios temporales y herramientas externas simuladas; los contratos de los runners con transporte simulado; y las pruebas de candado, solicitudes de gate, cancelación y evidencia de comprador.
- Incorporación: un proyecto sin perfil completa un comando compatible; un perfil agrega etapas y artefactos; un nodo adicional participa sin cambiar código consumidor; una herramienta o arquitectura faltante produce una explicación y no un arranque fallido oculto.
- Concurrencia: varios emisores no superan el presupuesto de un equipo ni reservan dos veces su BD o dispositivo. Windows y WSL consumen el mismo presupuesto físico. La idempotencia evita duplicados y rechaza claves con contenidos contradictorios.
- Prioridad: un urgente adelanta pendientes elegibles; no desplaza un activo no interrumpible; los equivalentes respetan FIFO; las cuotas y el envejecimiento dan servicio a prioridades menores con una llegada sostenida de urgentes. Se rechazan elevaciones no autorizadas.
- Pausa: solicitarla no libera capacidad; alcanzar un punto seguro conserva avance y libera únicamente lo confirmado. La reanudación vuelve a admisión. Un ejecutor sin soporte lo informa y continúa. Un intento reiniciable se repite sólo después de acreditar el cierre del anterior.
- Recuperación: interrumpir clientes y supervisores de prueba, reiniciar el coordinador y simular heartbeat vencido conserva trabajos y evidencia. Una carga dudosa bloquea la reasignación. Un dueño anterior no modifica una reserva de generación posterior.
- Evidencia: cambios locales y staged forman parte de la instantánea acreditada. Se detectan artefactos viejos, salida remota ausente, logs incompletos e incompatibilidad del entorno al reanudar. Un wait interrumpido no convierte la corrida en éxito ni la cancela por sí solo.
- Salvaguardas: se verifican identidades reutilizadas, separación de namespaces, recursos ajenos, nodos drenados y estados de salud inválidos. Los procesos usados para probar cleanup pertenecen exclusivamente al entorno de la prueba.
- Distribución: una actualización canary deja las corridas activas en su versión; un nodo incompatible no se activa; un fallo conserva la versión funcional; el rollback compatible conserva el estado. Una migración no permite que los clientes viejo y nuevo admitan el mismo recurso independientemente.
- Verificación sobre artefactos reales: antes de habilitar cada adaptador, realizar un smoke acotado en los nodos de prueba y comprobar comandos, aislamiento, recibos, pausa soportada y limpieza propia. Los contratos simulados no acreditan por sí solos interoperabilidad macOS, WSL o Windows nativo.
- Condición de terminado: un proyecto nuevo entra sin modificar Fleet; un nodo nuevo se incorpora por configuración y comprobación; las prioridades y pausas respetan la política; los clientes concurrentes comparten presupuesto; y una mejora se distribuye y revierte sin alterar corridas activas ni perder evidencia.

## Out of Scope

- Sustituir los gates o decidir qué pruebas requiere cada proyecto.
- Cambiar el routing de modelos, presupuestos o delegación de agentes de T3, Codex o Claude.
- Ejecutar cargas de producción, desplegar aplicaciones o reanudar programas pausados por una instrucción del usuario.
- Cambiar Windows Update, límites de WSL, pagefile, emuladores o servicios compartidos como parte del alta automática.
- Terminar procesos ajenos o activar limpieza nativa general de Windows.
- Garantizar pausa instantánea de cualquier test arbitrario o migración de su memoria a otra computadora.
- Construir alta disponibilidad del coordinador, múltiples schedulers activos o una plataforma de contenedores distribuida.
- Desplegar una interfaz gráfica completa o exigir un servidor MCP para consumir la cola.
- Reemplazar la configuración general de skills y agentes de PAO.
- Abrir un repositorio nuevo para Fleet en esta entrega.

## Further Notes

La base es la investigación y propuesta de esta conversación. Se revisaron los
lanzadores de comprador y apotheca, los slots de SEN-CheckIn, el instalador PAO,
el monitor Windows y los contratos de las sesiones que trabajan protecciones.
La investigación separa código en main, código en rama, diseños pendientes
y estados operativos reportados por otras sesiones. No midió los hosts en vivo.

La prioridad del trabajo es entregar incorporación común de proyectos y nodos,
admisión verificable y evidencia. La migración debe avanzar por unidades
comprobables y coordinarse con los dueños actuales de U1, U2, U3 y el launcher
Windows para no duplicar cambios ni dar por implementadas sus protecciones.

Las decisiones de implementación que no fijan conducta pública, como valores
iniciales de recursos, intervalos de envejecimiento y límites de ráfaga, se
calibran con el piloto y quedan en política configurable. La validación de
equidad usa políticas de prueba explícitas y no depende de esos defaults.

Felix confirmó el CLI como frontera principal de pruebas, complementado con
smoke real por plataforma, y pidió publicar en un proyecto nuevo de Linear
llamado exactamente `orchestation`. Esta especificación se publica con
ready-for-agent, sin otra ronda de triage. La configuración permanente del
tracker en el repositorio queda como trabajo separado de esta especificación.
