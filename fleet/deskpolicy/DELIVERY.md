# Entrega U1, corrección de ronda 2

Felix quiere usar el desk de noche para más corridas en paralelo. El reparto
entre Jale y Hasito varía según los turnos en espera y lo gobierna un archivo
de política PAO con vencimiento. El canario conserva 4 corridas y peso CPU 8.

Nivel alto por contrato entre servicios y reserva común. Se aplicó felix-mode
con el playbook Bug fix. No hubo descendientes, PR ni despliegue. La revisión
independiente posterior y la apertura del PR corresponden a PAO.

## Base y alcance

Se leyeron completos el brief U1, el brief de corrección r2, ambos diseños
normativos y las reviews sol y opus de ronda 1. El remoto y el checkout se
comprobaron en `dca130f5a850f210be1c6d10a2880bdfb1631c8a` antes de editar.
La rama local ya existía en la worktree anterior, por lo que el comando
`switch -c` falló. Se usó `git switch --ignore-other-worktrees feat/desk-politica-v1`
en esta worktree propia. No se editaron archivos de la worktree anterior.

La reserva sigue dentro del registro atómico de solicitud. El cambio de
ciclo de vida añade token de handoff, salida del guion y causa de cierre.
Model the Domain conservó separados la decisión de admisión y el resultado
del guion. Una claim incierta conserva recurso, CPUs y peso.

## Hallazgos y regresiones

Las pruebas nuevas están en `tests/test_r2.py`. Los nombres de la tabla
omiten el prefijo `test_`. El commit `eacea1a` registra primero los repros.
La documentación normativa común quedó en `adcc570`.

| Hallazgo | Commits de corrección | Regresión |
| --- | --- | --- |
| C1 | `99dac62`; fixtures en `5a93bd2` y `ddd3c9f` | `c1_uncertain_resource_stays_reserved_for_both_projects`, `c1_death_after_census_before_flock_keeps_active_reservation`, `c1_estado_reports_duplicate_without_changing_records`. Jale y Hasito conservan el recurso, sin claim nueva; otro carril sigue siendo admisible. El duplicado inyectado sigue visible como error 75 y no cambia archivos. |
| W1 | `7700117`, `b4ae402` | `w1_empty_boot_creates_private_state_and_admits_day`; la prueba W9 admite por CLI heredando el mutex. Raíz y subdirectorios 0700, mutex 0600, día sin política. |
| W2 | `30832a7`, `b3c54a6`, `6bf0695` | `w2_remote_run_0775_receipt_is_accepted`, `w2_invalid_receipt_preconditions_never_commit_claim`. Carpeta 0775 admitida; carpeta 0777 y recibos conflictivos o incompletos dejan cero claims. |
| W3 | `f6b1c24`, `87778b5` | `w3_script_exit_is_preserved_with_live_descendant`, `w3_stdout_reports_final_receipt_without_receipt_file`. Un guion sale 3 con hijo vivo; rc 3, recibo admitted, salida 3 y claim incierta con capacidad retenida, tanto en archivo como en el recibo final de stdout. |
| W4 | `8d7a53b`, `593cda9` | `w4_remote_runner_group_term_reaches_script_and_closes`, `w4_remote_runner_group_kill_reaches_script_and_closes`. TERM y KILL del grupo alcanzan al guion; salida 143 o 137 y cierre `stopped`. |
| W5 | `24f7be5` | `w5_keeper_closes_in_own_session_with_inherited_token`. El token incorrecto falla; el keeper en otra sesión con fd de vida y token heredados cierra. La prueba W9 pasa el token por fd a la CLI real. |
| W6 | `29367ec`, `adcc570` | `w6_expired_window_modes_and_documented_writer_temporary`. Ventanas vencidas legibles en 0600, 0644, 0664 y 0777 son inertes; un temporal `.pao-window-*` incompleto no invalida el censo. |
| W7 | `c07e4f8`, `9703c51` | `w7_oversize_rejected_before_fifo_and_reduction_expires_head`. Pesos 10 y 13 dan 75 `weight-too-large` sin solicitud. Reducir el máximo retira de FIFO una cabeza excesiva y permite al siguiente. |
| W8 | `c7752c7`, `adcc570` | `w8_day_projects_are_independent_of_weight`. Dos Jale y dos Hasito con suma 14 entran de día y reciben 12 CPUs. La suite original conserva presupuesto y afinidad nocturnos. El contrato fija la tabla de comandos y Turbo concurrency 4 para U2. |
| W9 | `aea1af2`, `b4ae402`, `f0713ca` | `w9_explicit_cli_harness_reaches_real_entrypoint`. CLI real con raíz, reloj, boot, loadavg y ruta de listeners privados; carga 9 rechaza Jale. Admisión y cierre con fds reales pasan. Sin la llave del harness no se aceptan costuras ni código fuera de instalación. |
| W10 | `a0ca557`, `f0713ca`, `dc16bae` | `w10_cutover_prevalidates_historical_locks_and_is_idempotent`, `w10_failed_publication_rolls_back_and_retry_preserves_state`, `w10_concurrent_destination_change_is_revalidated_under_mutex`. Locks 0644 fallan antes de instalar. Tras chmod conservan inodes; instalación repetida idéntica, rollback de error inyectado, reintento sin reemplazar evidencia y rechazo de un destino concurrente distinto bajo el mutex. |

No se rebaten C1 ni W1–W10. Los cuatro nits se declinan como manda el brief.
No se añadió recolección de tombstones, liberación forzada de inciertas ni
cambios a hostgate. Los cambios del wrapper resuelven W4.

## Pruebas en el desk

Todas las corridas usan `REMOTE_RUN_HOST=desk remote-run`, una a la vez,
`taskset -c 0-1`, gate previo de carga menor de 12 y 120 s desde el último
cambio de carpeta de runs. El guion repite los gates al arrancar.
Todos los fixtures viven en el mirror. La MacBook sólo editó código.
No se escribió en `/tmp/sen-test-slots` ni en estado comprador real.
Las pruebas del instalador llaman al helper con destinos privados de fixture.
No ejecutan `deploy-desk.sh` ni `withdraw-desk.sh`.

| RunId | Artefacto | rc y resultado |
| --- | --- | --- |
| `20261104-234756-08ee37` | Runtime original, pruebas de `eacea1a` | 1. Las 28 originales pasan. Total 42; 10 fallos y 5 errores en las regresiones nuevas. |
| `20261104-235729-570bad` | Runtime y pruebas de `f0713ca` | 0. 44 pruebas OK en 960.836 s y `bin/pao check` correcto. |
| `20261105-001608-85e3f9` | Intento sobre `937621c` | 1. Gate interno de espaciado, sin ejecutar pruebas. |
| `20261105-002203-3da4ce` | Runtime y pruebas de `be5b12a` | 0. 44 pruebas OK en 431.592 s y `bin/pao check` correcto. |

El primer gate midió carga 11.0703 y 339.08 s. El segundo gate midió carga
9.2910 y 153.22 s. Antes del intento final se midieron carga 8.6655 y
149.05 s, pero el gate interno rechazó el espaciado respecto de otra carpeta
de runs. Se conserva el intento fallido y se reintenta sin relajar el gate.
El gate de la segunda corrida corregida midió carga 5.7480 y 145.18 s.
Los logs completos están en `evidence/<runId>.log`. Sólo se normalizan
los espacios al final de líneas de progreso de unittest para `git diff --check`.
Los errores de la reproducción incluyen el helper de instalación y el API de
token aún ausentes. La primera versión del fixture de carrera llamaba desde
una sesión no válida; `5a93bd2` la ejecuta en una sesión real y cubre la muerte
entre censo y flock. No se presenta el error anterior de fixture como prueba
de esa variante de C1. La reproducción de claim incierta sí mostró admisión
indebida y `duplicate-resource` con el runtime original.

La suite completa incluye además sintaxis Python 3.9, sintaxis de los tres
guiones, `bin/pao check` y ShellCheck si está instalado.
Los gates e integración final de Jale y Hasito siguen en sus unidades.

## Corte y regreso

El contrato documenta el drenado, mutex histórico, chmod de los mismos inodes,
archivo de metadatos sin borrar, instalación de día, evidencia, armado y canario.
El instalador prevalida antes de publicar runtime o configuración. Una
instalación idéntica es reejecutable y un error detectado devuelve los nombres
nuevos al staging. Las pruebas inyectan un error después del commit de
configuración y acreditan rollback y reintento. No simulan un corte eléctrico.

El regreso publica día, conserva todas las reservas y drena antes de revertir
consumidores o binarios. El módulo no recupera por TTL una claim incierta.
`taskset` no limita Docker Desktop ni trabajo que escape deliberadamente.
La noche requiere política válida de PAO, preflight ligado al boot y código,
y arranque explícito del escritor. El día se recupera de una raíz ausente.

Los esquemas r2 añaden campos obligatorios a la claim y al recibo. El corte
exige drenado y consumidores coordinados. No hay migración automática de
claims de ronda 1 ni adopción de carga legacy. No quedan preguntas de producto
abiertas para esta corrección. PAO conserva el APTO operativo y la activación.

## Archivos cambiados respecto de la ronda 1

- `fleet/contracts/desk-politica-v1.md`.
- `fleet/deskpolicy/README.md` y este `DELIVERY.md`.
- `fleet/deskpolicy/cli.py`, `engine.py`, `model.py`, `store.py`, `testing.py`.
- `fleet/deskpolicy/installation.py` y `deploy-desk.sh`.
- `fleet/deskpolicy/tests/test_policy.py` y `tests/test_r2.py`.
- Logs de las corridas de ronda 2 en `fleet/deskpolicy/evidence/`.
