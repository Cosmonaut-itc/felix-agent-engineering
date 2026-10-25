# SEN-3820 r2 — matriz pública, cero ejecución

Inventario obtenido por texto, sin discovery/imports: **58 métodos** en un único
TestCase `FleetCLI`:16 base en `test_cli.py`,27 en `Core3820Cases`,15 en
`Operator3820Cases`. Se conservan los53 r1 (16+24+13), se añaden cinco y se refuerza
C02 dentro de su método original. Discovery futuro sólo `test_cli.py`, según README.
**34 filas padre =26 definidas +6 parciales/condicionales +2 positivas abiertas**.
Los3 requisitos adicionales no cambian esa partición. No se cuentan métodos como
subcasos ni código escrito como cobertura runtime. Todos siguen sin ejecutar.

Cada nombre siguiente es un método real completo, con prefijo `test_`. C reside en
`core3820_cases.py`, O/A/B en `operator3820_cases.py`. Recursos comunes: directorio
físico propio0700 `FLEET_TEST_RUN_BASE`, estado/proyecto/HOME propios, CLI en procesos,
JSON/códigos originales, transiciones y barrera con watchdog fallido30s. El comando
counted escribe/fsync un contador externo fuera del origen capturado. IDs iguales
por sí solos no prueban una ejecución. Sólo procesos de fixture reciben señales.
Ninguna prueba importa Fleet, escribe SQL o inyecta respuestas/cierre en producto.

| Fila / clasificación frozen | Método (prefijo test_) | Subcasos y oráculo escrito; límite |
| --- | --- | --- |
| C01 / definido | c01_equal_concurrent_admission_and_external_single_entry | Dos clientes con barrera externa de inicio: new/replay, IDs/admission/input originales, contador1, wait0 tras final. Carrera acotada; no acredita interleaving interno determinista. |
| C02 / definido | c02_different_concurrent_content_conflicts_without_second_entry | r2: correspondencia argv/etiqueta/CompletedProcess/respuesta0/5; existing original y rechazo explícito. Contador externo exactamente [etiqueta ganadora] durante running y al terminar. Stdout y artifact winner.txt publican sólo esa etiqueta; evidence/manifiesto y hashes ligados a job/attempt/input ganadores. Carrera acotada, sin ganador predeterminado ni intercalación interna acreditada. A5 corrige el oráculo, no demuestra ejecución errónea r1. |
| C03 / definido | c03_discarded_submit_stdout_recovers_by_key_and_replays_once | Toda salida submit a DEVNULL; otro status K atribuye admisión, replay exacto conserva IDs, contador1. ACK perdido, no caída en commit. |
| C04 / definido | c04_lookup_after_origin_removed_and_candidate_rejection_is_scoped | Origen retirado tras running; status/wait/evidence con env diferente resuelven original; nuevo candidato imposible2/not_evaluated, asociación conservada. |
| C05 / definido | c05_finished_success_and_failure_replay_never_execute_again | Exit0 y7 separados: wait0/1 con complete; replay conserva recibo y contador1. |
| C06 / definido | c06_lost_supervisor_and_empty_group_do_not_prove_terminal | SIGKILL a supervisor propio tras running, gate termina comando, ausencia de grupo observada; A incierto, B bloqueado owner_uncertain, replay0, wait/recover3, sin otra entrada. |
| C07 / definido | c07_sigint_wait_only_interrupts_reader | Running en barrera; acuse externo importtime del lector, SIGINT130/client_interrupted, transiciones iguales; luego final independiente. Importtime no localiza una transacción. |
| C08 / definido | c08_client_group_loss_preserves_original_supervisor | Wrapper cliente en sesión propia y SIGHUP a su grupo tras ready; supervisor original y running siguen; cierre normal y contador1. No SSH. |
| C09 / definido | c09_timeout_only_for_safe_pending_and_uncertainty_precedes_it | Running da4; dueño perdido da3 antes de timeout0. Ventanas terminal/publication/release pendientes sólo si se alcanzan externamente (C20/O07); no acreditadas aquí. Baseline wait registra/reintenta únicamente la ventana finished/publication pending para esperar complete. |
| C10 / definido | c10_enqueue_only_survives_and_two_recoverers_claim_once | Enqueue-only queued seguro y dispatch_not_requested; replay no despacha; dos nuevos recoverers, un claim durable/contador1. Se tolera3 transitorio de un observador concurrente starting sin vínculo. |
| C11 / definido | c11_results_survive_new_coordinator_processes | Exit0/7, procesos nuevos status/evidence/recover; comando/huella/IDs persistidos, wait0/1, contador1. |
| C12 / definido | c12_recover_preserves_live_owner_and_does_not_adopt_lost_owner | Dueño vivo acreditado permanece, recover no claim; variante supervisor perdido retiene/recover3. Pérdida del coordinador separado también en C08. |
| C13 / definido | c13_effective_bytes_catalog_mode_head_and_index | Repo propio: bytes, catálogo y bit exec distintos dan5; sólo índice diferente replay; commit vacío cambia HEAD y da5. Contador externo no aumenta. |
| C14 / definido | c14_private_exclusion_secret_equality_and_undeclared_negative | Privado mutado replay; credencial ficticia cambiada5 sin valor/digest público; archivo ordinario no declarado cambia conflicto y sí viaja al log; stdout/argv con secreto declarado se redactan. |
| C15 / definido | c15_argv_environment_outputs_declarations_client_and_pwd | Variaciones separadas argv/PATH/volátil/presencia env/artifact/credential-env/client dan5; sólo PWD derivable replay; PWD declarado credencial cambiado da5. |
| C16 / definido | c16_precommit_rejection_does_not_bind_key | Autoridad inicializada; input inválido2/candidate false/not_evaluated, status K not_found con revisión; después K válido new. |
| C17 / definido | c17_absence_is_revision_and_authority_scoped | Not_found antes de envío y admisión posterior con revisión mayor; lector no inicializa otra autoridad; mismo K nuevo en otra autoridad tiene UUID distinto. La ausencia no pronostica una carrera futura. |
| C18 / definido | c18_missing_artifact_signal_and_positive_never_created | Exit0 sin artifact: cierre original, collection incomplete, retained, wait/recover3 y replay histórico; señal propia SIGKILL conserva signal/no exit137 y wait3 aun released; ejecutable inexistente: never-created positivo, bundle vacío explícitamente N/A, complete/failed/wait1. |
| C19 / definido | c19_post_completion_payload_loss_does_not_rewrite_history_or_reacquire | Retirar artifact después de complete; sustituir manifest de un segundo job por el primero, conservando copias: recorded succeeded, outcome incomplete/evidence false, wait3 y released permanece. Recover no readquiere capacidad. |
| C20 / condicional | c20_real_quota_failure_at_terminal_collection | Cuota externa aislada preexistente: running antes del llenado propio acotado, errno ENOSPC/EDQUOT, abrir gate y observar cierre/incierto; restaurar sólo espacio propio, wait/recover3 y contador1. Falta acreditar cuota y frontera realmente alcanzada; admisión fallida no cubre cierre. |
| C21 / ABIERTO positivo | c21_host_backend_rejected_without_claiming_positive_generation_coverage | Sólo submit backend host-reservation@1 rechazado2/backend_unsupported/not_evaluated, sin crear estado. Falta integración capaz de entregar/procesar cierre ordinario G con H activo. No API simulada ni test positivo. |
| C22 / definido | c22_readers_and_replay_leave_original_authority_unchanged | Running en barrera y luego supervisor perdido con grupo terminado; status/evidence/wait/lector fallido con TMPDIR/TEMP/TMP apuntando a estado y alias; inventarios sin atime idénticos en ambas vistas. Replay con env original conserva transiciones, payloads originales y contador; permite sólo preparación propia atribuible. |
| C23 / parcial/condicional | c23_authentic_incompatible_cli_states_and_retention_preconditions | CLI auténticos2/3 en copias propias crean estados activos y cerrados; nuevo status por JOB/K, wait, evidence, recover, submit con/sin K rechazan2/unsupported_schema con inventario sin cambios. Historial auténtico>7d consulta K y fecha real; sin reloj inyectado/SQL. Faltan copias CLI provistas e historial auténtico; edad uncertain separada no acreditada por un historial finished. |
| C24 / parcial | c24_public_queued_and_running_crash_campaign_preserves_one_permission | Queued público, recover, running, replay y contador1; supervisor propio perdido tras running retiene/recover3. C08 añade muerte de cliente. Claim/vínculo/compuerta/Popen/fsync/commit exactos no controlados ni cubiertos por esos estímulos. |
| O01 / definido | o01_positive_local_resolution_keeps_failed_evidence_and_drained_fifo | Cierre original real con artifact faltante; B queued; drain/assess/release auditados, actor nativo, proof, errores e incomplete conservados, drained y B sin entrar. Sólo reopen+recover permite contador B1. |
| O02 / definido | o02_empty_group_without_original_terminal_and_self_assertions_cannot_release | Supervisor perdido y grupo terminado; assess insuficiente3/eligible false, proof inventado2, proof negativo3 y --proof-file JSON rechazado2. PID/TTL/done/contención autoafirmados no autorizan release. |
| O03 / condicional | o03_reason_and_declared_actor_rejections_with_real_os_actor | Motivo ausente/vacío/espacios/control/largo y flags --actor/--uid rechazados2; USER ficticio no cambia uid/cuenta/birth nativos. Otra cuenta real y denegación real de observación nativa requieren ejecutores externos preexistentes: no hay fixtures equivalentes ni cobertura positiva de esos negativos. |
| O04 / definido, operador | o04_new_old_generation_release_is_processed_and_rejected_before_proof | G liberado, H running, C queued, drain nuevo; nueva operación para G da5/generation_mismatch, validation_processed auditado, H intacto. Replay G devuelve historia sin tocar H; C sólo tras cierre/reopen/recover. No cierra C21. |
| O05 / definido | o05_operation_binding_replay_conflict_and_concurrent_release | Dos releases mismo ID convergen new/replay con receipt igual; motivo distinto5/audit; dos IDs distintos de otra reserva producen committed/already_released, un release efectivo. |
| O06 / condicional | o06_real_quota_failure_cannot_commit_capacity_without_audit | Dos cuotas/fixtures propias: llenar después de assess antes de release y después de release antes de reopen. Rechazo2/3; restaurar sólo relleno propio y consultar not_found/retención o committed con auditoría/capacidad/control correlacionados si ACK fue incierto. Falta cuota aislada y acreditar fallo en operación, no sólo llenar. No inyecta cada fsync. |
| O07 / parcial | o07_lost_stdout_recovers_each_atomic_operator_binding | Descartar stdout de los4 verbos, status y replay recuperan receipt; segunda asignación terminal, SIGKILL a clientes propios después de importtime y consulta/repetición explícita. Registra si señal se envió y binding observado. Frontera exacta intención/commit/fsync no probada; importtime/ACK no se presentan como tal. |
| O08 / definido | o08_only_explicit_current_epoch_reopen_allows_dispatch | Release incompleto sin reopen; evidence/recover no abren ni B entra. Reopen explícito; nuevo drain D2, replay viejo devuelve historia, nueva operación D1 da5. Reopen D2+recover permite B1. |
| O09 / definido | o09_old_epoch_proof_and_cross_authority_proof_cannot_change_current | Tras release/reopen/drain nuevo, misma asignación ya libre responde already_released sin repetir release. Proof de otra autoridad contra destino todavía retenido da2/proof_invalid; proof propio sí libera. Mismatch prioritario con H en O04. Proof_stale exige cambio relevante acreditado, no se simula alterando revisión. |
| O10 / ABIERTO, fuera3820 | o10_host_side_operator_backend_is_explicitly_unsupported | Sólo operator_backend_unsupported2 auditado, sin drenar ni soltar. Positivo host-side depende de dueño/interfaz/prueba autorizada futura3822/3823; sin fixture/API ficticia. |

## Requisitos adicionales

| ID | Método (prefijo test_) | Oráculo y parte pendiente |
| --- | --- | --- |
| A-W1 | a_w1_live_drain_blocks_reopen_and_ordinary_supervisor_handoff | A running causal, B queued → drain D → reopen prematuro3/reopen_blocked → cierre ordinario A completo/released, B permanece queued durante D → reopen nuevo+recover → B una vez; audit/transiciones/counters. A6 diferido: released no acredita que el supervisor haya recorrido el relevo antes de reopen; no cierra cobertura causal de esa guarda. |
| B-W1 | b_w1_sigint_dispatch_campaign_records_actual_observable_window | Tres submits propios, observar admisión/estado/vínculo, SIGINT sólo si cliente vive, salida0/130 y admisión durable; gate, terminal/released y contador≤1. Registro explícito exact_pre_spawn_proven=false. Starting/unbound no prueba que Popen no ocurrió. Falta control externo causal del hueco preciso claim→spawn, sin hooks/SQL/sleeps que lo finjan. Política de producto sí está implementada y descrita en SEN-3820.md. |
| B-W2 | b_w2_atomic_local_binding_not_found_committed_ack_loss_and_conflict | Status not_found, drain con stdout perdido, committed/audit, replay receipt igual, cambio5; recover operator_pending vacío y drained, segundo ID already_drained0 misma época. Rollback/cuota en O06 y muerte acotada en O07; no pending durable en modelo local. |

## Regresiones r2 adicionales

Los nombres llevan prefijo `test_`. La trazabilidad estática exacta contra0b,
incluida la diferencia de A5, está en [SEN-3820-r2.md](../SEN-3820-r2.md).
Estas cinco adiciones no reclasifican las34 filas contractuales.

| Hallazgo / módulo | Método | Subcasos escritos y discriminante |
| --- | --- | --- |
| A1 / core | r2_a1_complete_selections_validation_and_true_equivalences | 6 estímulos tras setups: punto+foo5 sobre bytes iguales; automático/punto duplicado/reordenación con duplicados replay; punto+missing y punto+.env2/not_evaluated. Original/contador intactos. 4 invocaciones subTest. |
| A2 / operator | r2_a2_conflict_audits_entire_denied_request_without_rebinding | 3 conflictos con otra asignación auténtica (release/assess/reopen), todos los selectores/prueba/época distintos; request íntegra/redactada y5/efectos nulos. Motivo secreto2 auditado sin binding;3 backends malformados2 sin auditoría; replay G conserva receipt ante H retenida. 6 invocaciones subTest,8 estímulos de estas categorías. |
| A3 / operator | r2_a3_pages_exhaust_divergent_operations_and_audit | 1 recorrido: una operación+3 conflictos sólo audit, límite2, ambos cursores, orden/identidades/listas exactas, avance y fin; página vacía, selección exacta e inventario iguales. |
| A4 / core | r2_a4_reader_open_and_selector_failures_are_read_only | 19 estímulos:4 lectores ×2 aperturas (ausente/privacidad insegura),3 lectores ×3 selectores erróneos, operation_not_found y paginación inválida. Todos2/read_only/sin observation; no crear autoridad, inventario idéntico. 17 invocaciones subTest. |
| A4 / core | r2_a4_real_directory_lock_distinguishes_readers_and_writer | 4 lectores concurrentes bajo flock EX externo causal devuelven2/read_only/state_lock_timeout;1 escritor distinto conserva3/unknown. Gate/finally, recursos propios, watchdog60s, ausencia de binding e inventarios idénticos. Ningún ledger escrito por fixture. |

Cinco métodos nuevos y27 invocaciones `subTest` nuevas por expansión estática de
bucles; recorridos/aserciones sin subTest no son métodos adicionales. Todos escritos,
cero ejecutados. Esquema incompatible sigue en C23 condicionado a CLI auténticos;
corrupción SQLite real/EACCES de apertura no tienen nueva fixture en r2. Su manejo
se inspeccionó; no se fabrican bytes de ledger para cerrar esos escenarios.

## Prerrequisitos externos y registro futuro

No se han creado ni comprobado recursos de ejecución durante esta entrega.
La futura corrida debe conservar entorno/certificación/head e intérprete exactos.
Ausencia de un prerrequisito falla explícitamente; no hay skips que sumen cobertura.

| Condición | Variables/entrada futura | Parte no acreditada |
| --- | --- | --- |
| Directorio de corrida | `FLEET_TEST_RUN_BASE`, físico, owner actual0700 | Sin él ninguna fixture arranca. No usar residuos3821 ni un temporal compartido. |
| Cuota aislada propia | `FLEET_TEST_QUOTA_BASE`, `FLEET_TEST_ISOLATED_QUOTA=1`, `FLEET_TEST_QUOTA_FILL_LIMIT` positivo ≤1GiB | La declaración no certifica filesystem: el dueño debe aportar aislamiento y límite seguro. Sin ENOSPC/EDQUOT real o sin fallo en fase objetivo, C20/O06 no cubiertos. No crear/montar volúmenes. |
| Esquemas auténticos | `FLEET_TEST_SCHEMA2_CLI`, `FLEET_TEST_SCHEMA3_CLI`, copias públicas completas de CLI reales | No se siembran tablas/versiones. Inventarios activos sólo con comando en barrera sin escritor antiguo avanzando. |
| Retención edad | `FLEET_TEST_AGED_AUTHORITY`, `FLEET_TEST_AGED_KEY`, autoridad propia e historial auténtico>7d | Sólo consulta; no registra admisión ni limpia/modifica ese estado externo. Falta edad incierta si el historial sólo es finished. No existe clock flag del producto. |
| Negativos OS O03 | Otra cuenta/ejecutor externo y entorno que deniegue realmente identidad nativa | No implementados como sustituto env. Sin ellos los dos negativos permanecen bloqueados. No crear cuentas/permisos globales. |
| Ventanas internas | Control causal externo real, aún no identificado en CLI | Claim/vínculo/R/compuerta/Popen/fsync/commit exactos y B-W1 pre-spawn siguen pendientes; campañas registran sólo lo observado. |

`cleanup` registra todas las admisiones antes de submit; sin stdout usa K. Rechazo
candidato confirmado no se confunde con ausencia de asociación histórica.
Incertidumbre/atribución faltante no autoriza borrar. Se abren gates de fixtures y
se recogen clientes; se exige ausencia observable de supervisores/grupos publicados.
Snapshots, campañas y contadores se guardan con fsync fuera del root eventualmente
retirado. Antes de retirar una fixture completa se conservan también bytes de
manifiestos/payloads públicos, cotejando hash e identidad estable sin atime; se
sincroniza el padre de evidencia. Un error impide retirar originales.
Estados con completion incomplete se retienen, incluso released con evidencia
posteriormente inválida. Una cuota propia fuera del root común se conserva y figura
en owned_bases. C23 conserva snapshots públicos antiguos en el informe; el historial
aged ajeno a la fixture es sólo lectura.

## Conservación de las16 pruebas base

Se mantienen aceptación/identidad; client; argv/cwd/stdin/logs; exit/señal; FIFO y
concurrencia; entorno/PATH por emisor; descendiente en grupo; atribución antes de
limpieza; estado público rechazado sin cambios; timeout/SIGINT del lector;
aislamiento de señales; supervisor perdido; fallo de lanzamiento; dos estados;
errores/log perdido/selección; default/reservados.

Cambios intencionales versionados: JSON en observation y errores tipados; cwd ahora
materializado; fallo de supervisor conserva accepted histórico (método renombrado);
finished/publication pending da3 y helper base sólo reobserva esa ventana; run base
obligatorio, watchdog30s y conservación de evidencia/incompletos. No se elimina un
caso para acomodar un resultado del runtime: no se ha ejecutado ninguno.
