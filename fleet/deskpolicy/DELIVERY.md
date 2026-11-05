# Entrega U1, corrección para ronda 3

Felix quiere más corridas nocturnas en el desk, con reparto variable entre
Jale y Hasito y reservas que no pierdan resultados ni capacidad tras fallos.
La corrección parte de `d12722acb827ad841a0aff56288a4c8d5575d449`, comprobado
contra origin después de fetch. Se usó la rama existente en esta worktree
con `git switch --ignore-other-worktrees`, sin editar la otra worktree.

Se leyeron los briefs U1, r2 y r3, los diseños normativos y las reviews r1/r2.
Nivel alto por contrato entre servicios. Se siguió Bug fix: reproducción,
pruebas primero, corrección y verificación en el mismo Linux. No hubo
subagentes, PR, despliegue ni escrituras en las dos rutas prohibidas.
La review independiente y el APTO operativo corresponden a PAO.

## Cambios

- Recibos iniciales, replays y terminales se publican dentro del mutex. Un
  replay relee el recibo vigente, conserva la identidad admitida y nunca
  pierde una salida o causa conocida. La claim cerrada y el fd de vida libre
  quedan visibles juntos al soltar el mutex.
- Una vez recogida la salida del guion, los errores de cierre se diagnostican
  con `pao/desk-error@1` por stderr y el wrapper devuelve esa salida. Una
  segunda toma del mutex procura persistir incertidumbre, salida y
  `close_error`. Si falla escribir la claim pero el recibo es escribible,
  se conserva allí el resultado. Sin mutex no se escribe ninguno de ambos.
- `reconciliar` conserva las reservas con sesiones vivas o flock tomado.
  Sólo prueba vacío completo, sin eximir al supervisor original. Registra
  `reconciled`, sin perder peso, CPUs, recurso ni salida conocida. Los
  metadatos de slots Jale cerrados se archivan por rename para no aparecer
  después como carga legacy. No se toca el inode del flock. El primer cierre
  desde un keeper registra su sesión opcional `keeper_sid`, que también se
  comprueba al reconciliar. Cada publicación ejecuta reconciliación bajo la
  misma toma del mutex; desactivar noche persiste antes de comprobar el censo.
- `admitir --lane any` elige el menor carril permitido sin claim no cerrada
  de su proyecto. El wrapper lo abre bajo el mutex. La CLI de bajo nivel
  acepta un mapa `--life-fds LANE:FD ...` heredado del supervisor para retener
  el flock elegido después de que termine la CLI. Jale conserva carril
  concreto y fd 8; el contrato exige probar los carriles en orden y pasar al
  siguiente ante `resource-busy`, sin limpiar slots inciertos.
- El contrato permite el bootstrap idempotente de raíz y locks por `estado`,
  sin crear claims, solicitudes ni políticas. Incluye las tres obligaciones
  de U2: cola del mismo hilo antes de verify, allowlist de symlinks para
  `db:gen` y probes de `/proc` o `fuser -m` en vez de `lsof +D`.

## Hallazgos y pruebas

Las regresiones están en `tests/test_r3.py`. Los commits `b4c5315`, `de52363`, `c26c837` y `7b69d6a` registran las
pruebas antes de los arreglos. Las dos regresiones
C1 de r2 ahora dejan un hijo vivo: una reserva realmente vacía ya tiene el
camino normativo de reconciliación. Sus hijos pasivos se fijan a CPU 0,
aunque el censo de prueba contabilice otras CPUs. No se desactiva la
reconciliación para C1.

| Hallazgo | Commit de corrección | Fallo antes y resultado exigido después |
| --- | --- | --- |
| W11 / W-C | `221108d`; contrato `ae78fc3` | `w11_replay_never_overwrites_terminal_receipt`: antes archivo `(active, None, None)` frente a claim cerrada; después `(closed, 0, completed)`. `wc_census_during_final_receipt_has_no_untracked_lock`: antes `untracked-life-lock`; después `estado` y `publicar` concurrentes pasan. |
| W12 / N-2 | `221108d`, `efa0498` | `w12_real_close_mutex_timeout_preserves_exit`: flock real retenido 6 s; antes wrapper 75, salida null; después wrapper 3, `script_exit_code=3`, claim uncertain y peso 1. Casos separados de `process-owner`, OSError y excepción inesperada conservan salida y diagnóstico. También se prueba un fallo al persistir la incertidumbre. |
| W-A | `e6811bc`, `efa0498` | `wa_reconcile_dead_day_claim_restores_night_cpus`: antes queda abierta; después día caído cierra como reconciled y admite noche. `wa_reconcile_keeps_child_and_life_lock_then_closes` conserva al hijo vivo y archiva metadatos sólo tras su fin. La CLI manual se prueba con la sesión viva de un keeper y después de su fin. |
| W-B | `e6811bc`; contrato `ae78fc3` | Antes `any` falla en argparse; después el carril 1 incierto conduce al 2. Se prueba tanto el wrapper como `admitir` con fds candidatos heredados, reteniendo el flock 2. |
| N-1 y notas U2 | `ae78fc3` | Antes faltan las reglas documentales; después la regresión de contrato exige bootstrap idempotente, ausencia de claims nuevas y las tres notas. |

No se rebaten hallazgos aceptados. No se implementa Jale ni Hasito, ni se toca
hostgate. `--life-fds`, la sesión del keeper y el archivo de metadatos precisan
cómo cumplir los requisitos existentes; no amplían los presupuestos.

## Ejecuciones

Las pruebas corrieron en el desk mediante `REMOTE_RUN_HOST=desk remote-run`,
una corrida propia a la vez, con el runner bajo `taskset -c 0-1`. Antes de cada
start se comprobaron carga menor de 9 y 120 s desde el último `started`
ajeno, con fallback al mtime del directorio. El guion repite el gate dentro
de la corrida. Los fixtures viven bajo el mirror. La MacBook sólo editó y
preparó snapshots y hashes. No se ejecutaron instalador ni retirada.
En la primera suite completa, los hijos pasivos añadidos a C1 podían recibir
CPUs contables fuera de 0..1. Se corrigió el fixture en `61385e0` para fijarlos
a CPU 0; la suite final incluye esa corrección. La primera corrida no se
presenta como aceptación final de la restricción de afinidad.

| RunId | rc | Evidencia |
| --- | --- | --- |
| `20261105-082031-f71d5c` | 1 | Primer repro: 8 casos, 7 fallos y 3 errores. Reproduce recibo degradado, timeout con 75 y censo con `untracked-life-lock`. Los subcasos posteriores al primer fallo de N-2 quedaron afectados por el fixture y no cuentan como prueba individual de OSError o excepción inesperada. |
| `20261105-082538-5fadbb` | 1 | Primera corrección: 11 casos, 2 fallos de fixture por renovar justo al TTL en las pruebas de `any`; los otros 9 pasan. Se corrigió la renovación a pulsos de 30 s. |
| `20261105-082810-16cef4` | 1 | Snapshot exacto del runtime d12722a, con las 11 regresiones corregidas: 6 fallos y 5 errores. Esta reproducción sí aísla los tres fallos de cierre en fixtures nuevos. |
| `20261105-083041-ddbace` | 0 | Primera suite completa sobre ae78fc3: 57 pruebas OK en 318.179 s y `bin/pao check` correcto. |
| `20261105-083903-d6ab75` | 1 | Runtime d12722a con las 14 regresiones finales: 8 fallos, 5 errores y un pase (guard de desactivación que ya cumplía r2). El censo concurrente reproduce `untracked-life-lock`. |
| `20261105-084604-8eb1df` | 0 | Suite final sobre `61385e0`: 60 pruebas OK en 348.597 s, sintaxis Python 3.9 y shell, y `bin/pao check` correcto. Afinidad del runner `[0, 1]`; hijos pasivos C1 corregidos a CPU 0. |

Los logs completos viven en `evidence/<runId>.log`. Se quitan sólo espacios
al final de las líneas. `evidence/r3-regressions.sh` conserva el comando del
repro acotado. El baseline se genera con `git archive d12722a` y se copia
encima únicamente `tests/test_r3.py`; no se modifica su runtime ni contrato.
Las faltas de comandos nuevos producen errores esperados en ese baseline.
No se usan esos errores como sustituto de las aserciones de resultado final.

SHA-256 del módulo final (los siete archivos de `store.SOURCE_FILES`):
`0a7dd19b0967341e73e2319d43a98b4b628a4f3e232af5c42badd7e1b20304c2`.
El baseline conserva el SHA-256 r2
`92b0957b76e2c33b63dcd49f49b73193d26566c4978423a78cd28bfc35b60bae`.
El gate interno final midió carga 1.8828 y 199.855 s desde el último
arranque ajeno. El hash impreso por Python 3.12.3 coincide con el anterior.
El timeout real del mutex imprime `wrapper_rc 3`, `script_exit_code: 3`,
`claim_state: uncertain` y un diagnóstico `mutex-timeout` separado.
Las 14 regresiones finales pasan, incluidos ambos censores concurrentes.
Después de la suite, una inspección de `/proc/*/stat` y `/proc/*/cwd` por SSH
no encontró procesos no zombies con cwd bajo el mirror final:
`LIVE_MIRROR_PROCESSES []`. `git diff --check` también pasó.
Los commits posteriores al probado sólo registran esta entrega y sus logs;
no cambian runtime ni pruebas.

Archivos r3: `fleet/contracts/desk-politica-v1.md`; `fleet/deskpolicy/{cli.py,
engine.py,README.md,DELIVERY.md}`; `tests/{test_policy.py,test_r2.py,test_r3.py,desk-run.sh}`
y los logs y el guion de reproducción de `evidence/`.
No quedan preguntas de producto abiertas. Los gates de consumidores y el APTO
siguen en PAO, U2 y U3. La evidencia de ronda 2 se conserva abajo como historial,
no como estado vigente de esta entrega.

---

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
| `20261105-003127-75c302` | Runtime y pruebas finales de `a9de766` | 0. 46 pruebas OK en 449.386 s y `bin/pao check` correcto. |

El primer gate midió carga 11.0703 y 339.08 s. El segundo gate midió carga
9.2910 y 153.22 s. Antes del intento final se midieron carga 8.6655 y
149.05 s, pero el gate interno rechazó el espaciado respecto de otra carpeta
de runs. Se conserva el intento fallido y se reintenta sin relajar el gate.
El gate de la segunda corrida corregida midió carga 5.7480 y 145.18 s.
El gate final midió carga 4.8789 y 131.03 s. La corrida final acreditó Python
3.12.3 y afinidad `[0, 1]`. Su hash de los siete archivos del runtime es
`92b0957b76e2c33b63dcd49f49b73193d26566c4978423a78cd28bfc35b60bae`.
Los commits posteriores sólo registran documentación y evidencia, sin cambiar
ese runtime ni las 46 pruebas. Una consulta final de `/proc` no encontró
procesos no zombies con cwd en el mirror de esta corrida.
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
