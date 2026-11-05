# Entrega U1 de desk-politica

Objetivo de Felix: usar el desk de noche para más corridas en paralelo,
con reparto variable por pendientes entre Jale y Hasito y política PAO con TTL.

Nivel alto por contrato entre servicios y reserva común de capacidad.
Rama `feat/desk-politica-v1`, creada desde `origin/main` en `f98d410`.
No se abrió PR, no se ejecutaron guiones de despliegue y no se crearon descendientes.
PAO conserva la review independiente sol high y opus high y la apertura del PR.

## Flujo de trabajo

Se aplicó felix-mode con el playbook Feature para una unidad de implementación.
El diseño base y el veredicto ya estaban fijados. No se reabrieron.
La instrucción de no crear descendientes sustituye los pasos de delegación.
La review y la publicación de PR quedan en PAO por instrucción del brief.

El checkpoint de ejecución tiene estos límites:

- Los primeros pasos bloqueantes fueron leer los dos diseños normativos y crear la rama desde el remoto.
- Los archivos de contrato, módulo y pruebas pertenecen a una sola transacción de entrega. No hubo agentes paralelos.
- El estado mutable del desk se serializa sólo para preservar presupuesto, FIFO, claim y CPUs. La carga queda fuera del mutex.
- Una sola implementación mantiene el contrato común. Dividirla entre trabajadores habría duplicado decisiones ya tomadas y estaba fuera del encargo.

Se aplicó Model the Domain mediante una máquina de estados por solicitud y
una claim incluida en su escritura atómica. El recibo separa decisión de salida
del guion y hace explícito que una repetición no ejecuta.

## Precisiones y desviaciones

- La claim está dentro del registro de solicitud. El replace único acredita admisión y consumo de presupuesto a la vez. Evita una transacción parcial entre dos archivos.
- `last_admitted_boottime_s` se deriva de todas las claims del boot, incluidas las cerradas. No hay un archivo separado que pueda perderse después del commit de una claim.
- El censo legacy devuelve 75 cuando encuentra carga sin claim v1. No adopta por PID o mtime. El corte exige drenado y evita atribuir peso o CPUs inventadas.
- Los locks de vida Jale conservan los inodes históricos `slot-0.lock` a `slot-11.lock`. La CLI comparte el índice lógico 1 a 12 con Hasito y documenta la conversión `slot+1`.
- Se añadió `max_run_weight` para materializar el máximo por corrida que pide el brief. El fallback de día usa máximo 8 y 12 CPUs globales. El canario nocturno queda en 4 corridas y 8 unidades.
- No hay liberación forzada ni recolección de inciertas. El cierre automático sólo acredita una sesión cooperativa vacía. La recuperación externa requiere evidencia y acción operativa bajo el mutex.
- El gate de pruebas usa mtime de carpetas de runs como cota conservadora de arranque. También retrasa por cierres y evita leer archivos de estado ajenos que puedan bloquear.
- El armado conserva la configuración aprobada. Permite reducir presupuestos con cargas vivas; ampliar por encima de lo aprobado o activar noche desde un armado diurno exige drenado y un nuevo preflight.

## Archivos entregados

```text
fleet/contracts/desk-politica-v1.md
fleet/deskpolicy/README.md
fleet/deskpolicy/DELIVERY.md
fleet/deskpolicy/__init__.py
fleet/deskpolicy/model.py
fleet/deskpolicy/store.py
fleet/deskpolicy/engine.py
fleet/deskpolicy/cli.py
fleet/deskpolicy/testing.py
fleet/deskpolicy/deploy-desk.sh
fleet/deskpolicy/withdraw-desk.sh
fleet/deskpolicy/tests/__init__.py
fleet/deskpolicy/tests/run.py
fleet/deskpolicy/tests/desk-run.sh
fleet/deskpolicy/tests/test_policy.py
fleet/deskpolicy/evidence/20261104-232232-15378d.log
```

## Evidencia

Primera aceptación en el desk, runId `20261104-225823-305e6d`, rc 1.
El gate remoto acreditó carga y espaciado antes de comenzar.
Pasaron 20 de 21 pruebas. La carrera tuvo cero admisiones porque un solicitante
que no era cabeza conservó el mutex durante un fsync lento al guardar `fifo`.
Los otros 19 agotaron la espera de 5 s. No hubo exceso de capacidad.
El fixture inicial trataba el timeout esperado como excepción del hijo y
no reintentaba como un consumidor en espera. Se corrigió el fixture para
recoger un recibo denied con 75 y reintentar de forma acotada sin ampliar
el timeout de producción. La aceptación sigue exigiendo exactamente una
admisión y cuatro claims totales.

La primera suite tardó 1242.562 s. Se observaron esperas de Linux en
`jbd2_log_wait_commit` durante fsync. La duración no es una medida del costo
de producción, donde el estado vive en `/tmp`, y no en el mirror del repo.

La segunda aceptación usa runId `20261104-232232-15378d`, sobre código y pruebas
de `688825a41442ad19157662953942f0b884cabf11`.
Terminó con rc 0. Pasaron las 28 pruebas y `bin/pao check`.
La sintaxis Python 3.9+ y la sintaxis de los tres guiones son parte de la suite.
El log completo queda en `evidence/20261104-232232-15378d.log`.
El gate previo midió carga 1.5161 y 137.8 s desde el último cambio de carpeta
de runs. El script volvió a validar carga y espaciado en destino.
El runner acredita Python 3.12.3 y afinidad `[0, 1]`.
El hash del runtime probado es
`3cd34445235f3478685d997cafbc6bfb23e3716a6e3fe2400694106da9c2b228`.
Los cambios posteriores a ese head sólo agregan esta entrega y el log remoto.

La ejecución final no acredita integración desplegada, regresiones completas
de los consumidores ni review independiente. Esos gates corresponden a PAO.

## Prerrequisitos de integración

Los cambios Jale y Hasito, su handoff, FIFO, protección de mirrors, transporte,
recibos y evidencia de reportes se prueban en sus propios repos.
El APTO de PAO, un censo inicial vacío y el inventario de consumidores migrados
siguen pendientes antes de instalar o habilitar noche.
`taskset` no limita Docker Desktop ni escapes deliberados del mismo uid.
No se cambia CI ni se presume que esta entrega habilita admisiones actuales.

No quedan preguntas de producto abiertas para implementar U1.
PAO debe revisar las precisiones anteriores y aportar la evidencia operativa
del corte junto con los heads definitivos de Jale y Hasito.
