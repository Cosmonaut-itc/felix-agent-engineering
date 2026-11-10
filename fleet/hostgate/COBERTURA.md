# Cobertura del candidato Sol, ronda 4

Autoridad: `f6dea9a7afd182dff9742d52784dc4e98aeb451f`, PR #17, contenido idéntico a `a1f00fa`. Esta tabla distingue
implementación de sombra, simulación con harness y aceptación futura. No acredita
activación, integración de consumidores, aislamiento hostil ni corte real del runner.
Rutas relativas a `fleet/hostgate/`.

SEN-3822, corte 2a: `tests/test_admin.py` añade 9 escenarios compuestos P02/P03
y la porción administrativa de P10, sin endpoint ni cambios al CLI/Fleet:

| Garantía | Prueba de Administration |
| --- | --- |
| Digest del documento cargado, mapas profundamente inmutables y compatibilidad JSON/validadores | `test_load_revision_immutable_maps_and_existing_serializers` |
| Política modificada sin cambiar operatorRevision; lease vivo cierra | `test_policy_content_digest_and_live_revision_guard` |
| Locator fijo y rechazo de autoridad en payload; mínimos y volúmenes/recursos desconocidos por el core existente | `test_p02_fixed_locator_and_wire_stays_closed`, `test_loaded_minima_unknown_volume_and_resource_use_existing_core` |
| Schema estricto, bool/NaN/duplicados, permisos, ACL reales en archivo/ancestro, symlink/hardlink, owner/hardware ajenos y FS remoto/fallo de observación simulados | `test_strict_document_and_catalogs`, `test_real_permissions_links_acl_and_failed_observations` |
| B válido o con storeId clonado rechaza antes de SQLite; reducción en A conserva claves/débitos/exclusivos y proyecta duda | `test_anchor_other_store_and_reduced_budget_preserve_debits` |
| Competidor en proceso propio no toma lock; ancla/config/directorio/BD/lock sustituidos cierran; ausencia no recrea | `test_lease_competition_replacement_and_missing_ledger`, `test_live_config_anchor_and_directory_replacement_are_detected` |

Los negativos comparan inventario de archivos/directorios, inode, modos, tamaño,
mtime y contenido; no basta un código de salida. `comparison(base)` es un inspector
privado: main recibe JSON válido por su CLI y falla en `None != cfg_<digest>`;
el candidato carga archivos y verifica su lease antes de comparar revisión/host/store/
capacidad. No usa ImportError ni acredita un exploit de main. Fixtures dentro de la
copia de corrida, con política de owner y ruta explícita; separación root/cliente real,
IPC, observación de peers y activación quedan fuera de aquel corte. La investigación
posterior de SEN-5625 de lectura/WAL en 3.14 se documenta al final.
`tests/support/world.config` instala la seam privada de rutas en los procesos hijos
históricos: la copia remote-run está dentro de un checkout y no hereda mocks del padre.

R1-C1: `tests/test_admin_observation.py` reproduce una ACL real de 17 entradas y
ENOMEM sólo en realloc dentro de fstatx nativo (interposición C en un hijo propio).
Exige rechazo pese a rc=0, conserva archivo/metadata/ACL y acepta el control sin ACL.
También cubre metadatos filesec ausentes/incoherentes, entradas sin datos, errno
residual de éxito y salidas vacías de fstatfs64/sysctlbyname/gethostuuid.

SEN-3952 amplía la guarda de SEN-3900 a la serialización de la vista, el resultado/audit
y el contexto que se escribirá, todavía en sombra. El store usa los textos ya preparados:

| Garantía | Prueba en `tests/test_provider_failure.py` / alcance |
| --- | --- |
| La duda observada sobre A y su audit sobreviven al valor no serializable de B; C sigue rechazado al recuperarse A. Se retienen todos los débitos/exclusivos, sin cambios parciales ni `operations(B)`; el id puede reintentarse. | `ProviderFailure.test_sen3952_unserializable_provider_keeps_doubt_after_owner_recovers`: `nan`/`inf` del reloj y `bytes` del boot, tras una proyección válida, por `reservar` y `prepare` (6 subcasos). Exit 6 público; `prepare` relanza la excepción de serialización después del audit. |
| Confirmación fallida o ACK perdido tienen prioridad sobre el fallo del proveedor; no se consume el id de B. | `ProviderFailure.test_sen3952_unserializable_provider_confirmation_failure_is_uncertain`: mismos valores/rutas, con cortes antes y después del commit (12 subcasos). Exit 3 público; `prepare` propaga el fallo de persistencia. El corte posterior conserva duda/audit durables. |
| R1-W1: el contexto inválido conserva duda, audit y débitos/exclusivos; C sigue rechazado tras recuperarse A. | `ProviderFailure.test_sen3952_r1_context_keeps_doubt_after_owner_recovers`: anchor con `nan`/`inf`/`bytes`, por `latido`, `soltar`, `dudosa`, `reservar` y `prepare` (15 subcasos), con reintento del id de B. |
| Un resultado inválido tampoco pierde la duda, incluso sin operación registrada o al rechazar un dueño ajeno. | `ProviderFailure.test_sen3952_r1_result_keeps_doubt_after_owner_recovers`: los mismos valores sólo en el resultado, por tres mutaciones, conflicto de preparación y rechazo ajeno (15 subcasos). Se serializa el resultado real, después de quitar el envoltorio interno. |
| Los fallos de persistencia conservan su prioridad ante contexto o resultado inválidos. | `ProviderFailure.test_sen3952_r1_confirmation_failure_is_uncertain`: 48 subcasos, antes/después del commit; exit 3 público y excepción de persistencia en `prepare`. |

| Línea S del sketch | Archivo | Prueba / límite |
| --- | --- | --- |
| 02 | `__init__.py`, `cli.py`, `README.md` | `CLI.test_owner_payload_cannot_construct_auth`, `Contract.test_desk_no_transport_even_degraded`; Python 3.9 stdlib. |
| 21 | `admission.py`, `protocol.py`, `store.py` | `Contract.test_shadow_only_and_future_interfaces_closed`; ningún permiso efectivo. |
| 37 | `model.py:expire_pending`, `admission.py:_persist_effective` | `Storage.test_queries_never_mutate_sqlite_or_wal`, `test_replay_persists_expiry_without_renewing_or_regranting`, `Contract.test_jale_real_helpers_bound_to_three_preparations`; sin timer. |
| 56 | `admission.py:Hostgate`, `cli.py` | Casos de `Contract`, `CLI`; API sólo simulación, IPC futuro. |
| 72 | `cli.py`, `tests/support/world.py` | `CLI.test_owner_payload_cannot_construct_auth`, `CLI.test_public_cli_harness_and_strict_json`; endpoint sin auth rechaza. |
| 74 | `protocol.py:reply`, `admission.py:_run` | `Contract.test_shadow_only_and_future_interfaces_closed`, `Contract.test_replay_authentication_conflict_and_historical_state`; key dentro de `simulationKey`. |
| 90 | Estructura del paquete | Inspección `git diff --check`; sin módulos productivos F. |
| 91 | `README.md`, `__init__.py`, `__main__.py`, `cli.py`, `protocol.py`, `model.py` | `CLI`, `Contract`, `Storage`. |
| 92 | `admission.py`, `store.py`, `identity.py`, `scope.py`, `owner_record.py` | `Contract`, `Storage`, `Boundaries`. |
| 93 | `salud/__init__.py`, `salud/lector_desk.py`, `salud/lector_mini.py` | 112 casos originales desk adaptados a unittest, `MiniReader`; ejecutados en Mini, ver tabla de ejecución r4 al final. |
| 94 | `fixtures/jale-2254z*.json`, `fixtures/health` | Jale y lectores consumen fixtures; eliminados los tres JSON sin caller; no equivalen a aceptación nativa. |
| 95 | `tests/test_cli.py`, `test_store.py`, `test_contract.py`, `support/` | Suite unittest prevista por run.sh; pruebas escritas de clientes de varios procesos y SQLite real, ejecutadas en Mini, ver tabla de ejecución r4 al final. |
| 96 | `identity.py`, `scope.py`, `salud/lector_mini.py` | `Boundaries`, `Contract.test_scope_and_cleanup_all_or_nothing`, `MiniReader`; perfil/cierre real no acreditado. |

Las etiquetas S explícitas están en 02, 21, 37, 56, 72, 74 y 90; 91–96 desarrollan
la estructura S. Se añade cobertura de las reglas ejecutables sin etiqueta:

| Líneas | Archivo | Prueba / motivo de no cobertura real |
| --- | --- | --- |
| 01, 04–06, 08 | `README.md`, `model.py`, `store.py` | Configuración administrativa inyectada; `Storage.test_configuration_symlink_and_enforcing_rejected`. Persistencia/localidad del filesystem productivo no acreditada. |
| 07, 19 | `store.py` | `Storage.test_missing_corrupt_wrong_store_never_recreated`, `test_busy`, `test_commit_cuts_and_response_lost`. |
| 09–10, 52 | `identity.py` | `Boundaries.test_darwin_structure_and_live_identity_read_only`, `test_linux_exact_stat_parentheses`; lectura real Darwin acotada prevista y fixture proc Linux (`test_linux_proc_fixture_exact_token_reuse_zombie_and_missing`); ejecutadas en Mini, ver tabla de ejecución r4 al final. |
| 11–12 | `protocol.py`, `admission.py` | `CLI.test_demand_and_nul_validation`, `Contract.test_minimum_and_unknown_resource`, `test_budget_dimensions_and_exclusives`. |
| 13–14, 22–27 | `model.py`, `admission.py` | `Contract.test_jale_real_helpers_bound_to_three_preparations`, `test_channel_eof_withdraws_without_reservation`, `test_commit_revalidation_pid_anchor_channel_heartbeat`. |
| 15–18, 28–29 | `admission.py`, `scope.py` | `Contract.test_death_after_revalidation_retains_commit`, `test_budget_dimensions_and_exclusives`, `test_old_generation_never_alters_new`. |
| 20, 84–85 | `owner_record.py`, `README.md` | `Boundaries.test_owner_record_partial_and_root_terminal_protect`, `test_owner_fsync_failure_preserves_protection`. No cubierta integración U1: consumidor actual aún no entiende Fleet. |
| 30, 33, 40–42 | `scope.py`, `README.md` | `authorize_creation`/`record_started` fallan cerrados. No cubierto runtime de supervisor, permiso, publicación previa a efectos o reconciliación: F. |
| 31–32, 34–36 | `model.py`, `admission.py`, `scope.py` | `Contract.test_ttl_owner_lost_boot_and_heartbeat_cannot_release`, `test_scope_and_cleanup_all_or_nothing`, `Storage.test_queries_never_mutate_sqlite_or_wal`. |
| 43–50 | `scope.py`, `README.md` | Cierre positivo sintético fenced y casos negativos; `Contract.test_windows_fixture_only_positive_evidence_fenced`. No cubierta contención/launcher/cierre nativo real: F. |
| 51 | `README.md`, código sin control de procesos | Inspección y helpers propios con autoexit; no SIGSTOP ni terminación de procesos. |
| 54–55 | `admission.py`, `store.py`, `cli.py` | `Contract.test_replay_authentication_conflict_and_historical_state`, `Storage.test_commit_cuts_and_response_lost`; autenticación real espera F. |
| 57–62, 65–71 | `admission.py`, `model.py`, `salud/`, `cli.py`, `protocol.py` | `CLI`, `Contract`, `MiniReader` y 112 casos desk. |
| 73, 75–78 | `protocol.py`, `cli.py`, `admission.py` | `CLI.test_cli_all_commands_and_exit_mapping`, `Contract.test_replay_authentication_conflict_and_historical_state`. |
| 79–81 | `salud/`, `admission.py` | Lectores y `Contract.test_desk_no_transport_even_degraded`; transporte invocado cero veces. |
| 83 | `README.md` | No cubierta ordenación Fleet: pertenece al consumidor; hostgate muestra degradación y barreras sin admitirla. |
| 86–89 | `README.md`, `model.py`, fixtures Jale | Adaptador modelado, slots retenidos, boot drena. No cubierto corte/rechazo real de runners antiguos: activación F. |
| 98–100 | `tests/test_store.py`, `test_contract.py`, `test_cli.py` | Pruebas escritas de concurrencia por API y procesos CLI con presupuesto compartido; exclusivos, límites, replay, conflictos y generación vieja. |
| 101 | `tests/test_store.py`, `test_contract.py` | Cortes modelados antes de escritura/commit y ACK perdido; muerte postrevalidación. No cubiertos permiso/spawn reales: F. |
| 102–104 | `fixtures/jale-2254z*.json`, `tests/support/helper.py`, `test_contract.py` | Dos reproducciones modeladas, helpers reales EOF/legado acotado y variante que pudo crear. Adaptador run.sh real no cubierto: F. |
| 105 | `test_contract.py`, `test_identity_scope.py` | PID exacto/reutilización, EPERM modelado, latido, reloj/boot físico/ejecutor. |
| 106 | `test_store.py`, `test_identity_scope.py` | BD ausente/corrupta/storeId, busy, cortes/commit incierto; ENOSPC/fsync inyectado en publicación. No se llena disco real. |
| 107–108 | `test_contract.py` | Cierre/limpieza conservadores y Windows positivo sólo modelado. |
| 109–110 | `test_lector_desk.py`, `test_lector_mini.py`, `test_contract.py` | Corpus, Mini, falta de testigo y cero transporte. |
| 111–112 | `test_store.py`, `test_contract.py`, `test_identity_scope.py` | Consultas sin mutación, drenado por boot, pausa/slots/registro incompleto con protección. |
| 113 | Guion/evidencia de comprador | Sólo Mini por run.sh con turno; sin flags de producción. |
| 03, 38–39, 53, 63–64, 82, 97, 114–115 | `scope.py`, `cli.py`, `README.md` | F: no servicio/timer/IPC/supervisor/transporte/permiso productivos; interfaces disponibles rechazan. Sin activación por fixtures. |

| Corrección §5 | Archivo | Prueba / límite |
| --- | --- | --- |
| 1. Autoridad vigente | `README.md` | Inspección del merge contractual `f6dea9a7` / PR #17; Mini aceptada, no se pide nueva aceptación. |
| 2. Wire y generación | `protocol.py`, `admission.py`, `store.py` | `Contract.test_old_generation_never_alters_new`, `test_replay_authentication_conflict_and_historical_state`, `Storage.test_concurrent_budget_and_parallel_replay`. |
| 3. Preparación sin reserva tentativa | `model.py`, `admission.py` | `Contract.test_jale_real_helpers_bound_to_three_preparations`, `Storage.test_missing_corrupt_wrong_store_never_recreated`. |
| 4. Pausa y cierre conjunto | `scope.py`, `admission.py` | `Contract.test_scope_and_cleanup_all_or_nothing`, `test_positive_closure_evidence_is_durable`, `test_malformed_closure_provider_fails_closed`; todo o nada. |
| 5. Unix perfil + ámbito + linaje | `scope.py`, `README.md` | `Contract.test_scope_and_cleanup_all_or_nothing`; perfiles reales rechazados, garantía real pendiente F. |
| 6. Autenticación realizable | `model.py`, `cli.py`, `README.md` | `CLI.test_owner_payload_cannot_construct_auth`, `Contract.test_replay_authentication_conflict_and_historical_state`; transporte/peer real F no cubierto. |
| 7. Salud/transporte cerrado | `salud/`, `admission.py` | Lectores, `Contract.test_desk_no_transport_even_degraded`, `test_unknown_health_reason_is_barrier`; sin degradación admisible inventada. |
| 8. Motivos estables | `protocol.py`, `admission.py` | `Contract.test_budget_dimensions_and_exclusives`, `CLI.test_cli_all_commands_and_exit_mapping`; catálogo cerrado. |
| 9. Registro U1 | `owner_record.py`, `README.md` | `Boundaries.test_owner_record_partial_and_root_terminal_protect`, `Contract.test_supervisor_lost_with_descendant_alive_retains_all`; integración U1 no cubierta, requiere acuerdo versionado. |
| 10. Frontera real de migración | `README.md`, fixtures Jale | No cubierta: no se permite alterar/adaptar run.sh en este PR de sombra. Se documenta el corte antes de abrir Fleet. |
| 11. Jale, cinco condiciones/dos fixtures | `fixtures/jale-2254z*.json`, `test_contract.py`, `tests/support/helper.py` | `Contract.test_jale_real_helpers_bound_to_three_preparations`, `Contract.test_jale_independent_causes_and_possible_creation`; reproducción, no captura original. |
| 12. Tamaño y pruebas honestas | `model.py`, `store.py`, `cli.py`, `README.md` | `Storage.test_queries_never_mutate_sqlite_or_wal`, guion Mini y recibos; sin daemon/polling ni afirmaciones de IPC real. |


Registro histórico al cierre inicial de r2: dos corridas remotas con turno, ambas sobre árbol
limpio. `72a6d10` y `731d866` descubrieron 174 pruebas cada una; cada corrida tuvo
173 pruebas correctas y un fallo, sin errores ni omisiones. El primer intento de
lanzador `43d94850ecba47a88839709894495440` terminó con salida 130 esperando turno,
sin run_id ni pruebas. No hubo pruebas en MacBook.

| Familia / pruebas | Estado en Mini |
| --- | --- |
| CLI, 6 | Ejecutadas, PASS en las dos corridas |
| Contrato, 28 | Ejecutadas; Jale falló en `72a6d10` y pasó sus cuatro escenarios en `731d866`; las otras 27 pasaron en ambas |
| Identidad/ámbito/U1, 8 | Ejecutadas, PASS en ambas |
| Desk, 113 (112 originales + contraste) | Ejecutadas, PASS en ambas |
| Mini, 4 | Ejecutadas, PASS en ambas |
| Almacén, 15 | Ejecutadas; todas PASS en `72a6d10`; 14 PASS en `731d866` y fallo de orden en `test_two_executors_shared_world_budget_and_observation_changes` |
| Corrección `45b0e39` de esa aserción | No ejecutada en esas dos corridas; ejecutada después en `dad5ef8` (actualización fechada abajo). Identifica por executorId, sin depender del orden de commits |
| Inspección documental y sintaxis AST | Inspección local, no prueba runtime |

B-G1, B-G2 y B-G3 tienen pruebas escritas antes del código corrector. Fallaron sin
las correcciones en el commit `3f8ce69` reconstruido en Mini y pasaron en ambos SHA
corregidos. Ese rojo fue retrospectivo, no anterior a los commits correctores.
Para los otros cambios no se ejecutó rojo previo; Jale y orden concurrente sí tienen
fallos observados antes de sus correcciones posteriores. Al redactar ese registro aún no había suite verde sobre el head final; la tercera
corrida de la actualización fechada abajo supersede ese estado.

| Cambio obligatorio §6 / error | Archivos | Pruebas específicas añadidas o reforzadas |
| --- | --- | --- |
| 1 / B-G1, injerto 1 | `model.py`, `scope.py`, `admission.py`, `tests/support/world.py`, `tests/test_contract.py` | `test_b_g1_windows_proof_cannot_release_unix`, `test_scope_and_cleanup_all_or_nothing`, `test_windows_fixture_only_positive_evidence_fenced` |
| 2 / B-G2, injerto 4 | `admission.py`, `store.py`, `tests/test_contract.py`, `tests/test_cli.py` | `test_b_g2_replay_requires_exact_peer_and_channel`, `test_reserve_replay_and_query_role_same_peer_are_not_authority`, `test_heartbeat_sequence_replay_and_different_live_peer` |
| 3 / B-G3 | `admission.py`, `tests/test_contract.py` | `test_b_g3_revalidate_after_health_io`, `test_death_after_revalidation_retains_commit` |
| 4 / Jale, injerto 2 | `tests/support/helper.py`, `tests/support/world.py`, `tests/test_contract.py` | `test_jale_real_helpers_bound_to_three_preparations` (dos fixtures, cuatro escenarios), `test_jale_independent_causes_and_possible_creation` |
| 5 / B-M2, B-M3 | `scope.py`, `model.py`, `store.py`, `tests/test_cli.py`, `tests/test_store.py` | `test_malformed_provider_keeps_json_envelope_and_all_resources`, `test_valid_checksum_does_not_authorize_invalid_rows`, `test_sqlite_full_io_and_secondary_cleanup_preserve_uncertainty` |
| 6 / B-M1, B-M6 | `admission.py`, `tests/test_store.py` | `test_digest_conflict_persists_expiry_without_changing_original`, `test_state_reads_health_exactly_once`; coste de quick_check/reescritura sin medir |
| 7 / B-M4, B-M7, injerto 5 | `owner_record.py`, `tests/test_identity_scope.py`, `tests/test_contract.py` | `test_owner_root_identity_must_be_exact_and_separate`, `test_directory_fsync_after_replace_keeps_published_protection`, `test_owner_fsync_failure_preserves_protection`, `test_explicit_dudosa_retains_after_publication_failure`, `test_owner_rejects_production_symlink_and_stale_sequence` |
| 8 / B-M5, injertos 3 y 4 | `model.py`, `admission.py`, `store.py`, `tests/support/world.py`, `client.py`, `tests/test_store.py` | `test_two_executors_shared_world_budget_and_observation_changes`, `test_trusted_initial_drain_cannot_be_reopened_by_query`, concurrencia/replay con retry busy acotado |
| 9 / trazabilidad | `README.md`, `COBERTURA.md` | Inspección documental. Aceptación Mini ya existente; límites F conservados |
| 10 / evidencia | Guion y evidencia externa autorizada | Seis familias por unittest; estado según recibos. No cubierta activación |
| Desktop d8fecdc7 | `tests/test_lector_desk.py` | `test_observacion_powershell_contraste_d8fecdc7`: ausente, complete0, completewarn, partialunknown, unavailable, diagnosticutc; sano y errores superiores conservados |

No se acredita la liberación por perfil real Unix ni launcher Windows; sólo tipos,
fencing, retención y transiciones sintéticas. La inyección SQLite no es una prueba
VFS del fsync interno del ledger. Las pruebas de publicación no integran el consumidor
U1. El coste con historial largo sigue sin medir. Los tres fixtures JSON de configuración/identidad/launcher sin caller se eliminaron
en r3; no acreditaban ejecución nativa.


Recibos de las dos corridas: `36ec0d9c79bc4457824e988d186493e8` y
`41feb9cb75f0489c8ab00e2d1f76bc85` en la evidencia del encargo. SHA completos,
stdout/stderr y códigos están conservados. Último cambio funcional de producción
probado: `731d866f1c3461fd5bac00a9013c95abe077742b`; después sólo cambian una
aserción de prueba y documentación. Ese era el estado al cerrar las dos primeras corridas; véase actualización posterior.


Actualización fechada 2026-10-23 (America/Mexico_City), I-1 / A-M3: se conservan
las dos corridas fallidas anteriores y se registra la tercera, run
`20261023-192300-c75aef`, SHA `dad5ef88158daffe76a2a47566fec4924502da5d`.
Resultado: 174 pruebas / 0 fallos / 0 errores / 0 omitidas; CLI 6, contrato 28,
identidad/ámbito/U1 8, desk 113, Mini 4 y almacén 15. Las tres regresiones
B-G1/B-G2/B-G3 dieron rojo sobre `3f8ce69` en la misma corrida y verde sobre
`dad5ef8`. Evidencia:
`/Users/felixddhs/.claude/encargos/mini-carga/evidencia-hostgate-sol/9df0741daa4d4a629ac47bf2c4b6be5a/lanzador.log`.
Suite verde sobre ese SHA no equivale a aceptación técnica; las dos reviews
posteriores y el encargo r3 mandan sobre el trabajo aún pendiente.

| Punto r3 | Prueba / límite |
| --- | --- |
| 1, A-I1 | `test_a_i1_foreign_peer_cannot_consume_prepared_operation`, `test_a_i1_foreign_mutations_leave_ids_available_to_owner`; replay B-G2 y rechazo durable conservados. |
| 2, A-M1 | `test_valid_checksum_does_not_authorize_invalid_rows`, `MiniReader.test_malformed_fields_never_succeed_or_raise`, `test_huge_volume_and_clock_are_invalid`, `test_owner_huge_started_is_invalid_and_read_protects`, `test_desk_huge_integer_is_invalid_at_adapter_boundary`. Lector desk entonces intacto; A-M1 resuelto por adopción r4. |
| 3, M-2 | `test_boot_change_after_revalidation_keeps_ledger_usable`. |
| 4, M-1/A-M2 | `test_observation_skips_terminal_history_without_erasing_it`; siguen `quick_check` y reescritura completa, coste pendiente de medir. |
| 5, M-7 | `test_internal_provider_errors_are_not_request_errors`, `test_internal_query_failure_and_interrupt_keep_envelope`; incluye exit 130. |
| 6, M-8 | `test_state_wire_maps_granted_to_simulated`; sketch 25 interno, `simulated` público. |
| 7, M-4 | `test_closure_fencing_fields_and_never_created_each_fail_alone`; digest recalculado salvo el negativo específico de digest. |
| 8, M-5 | `test_pending_deadline_expires_before_heartbeat`; TTL de latido mayor que el de preparación. |
| 9, M-6 | `test_desk_no_transport_even_degraded` aísla DefaultShell con testigo válido. Renombrados `test_absent_reused_and_unverifiable_peer_is_unauthorized` y `test_explicit_dudosa_retains_after_publication_failure` según su alcance real. |
| 10, M-3/A-M4 | `test_observacion_powershell_contraste_d8fecdc7`: seis variantes y dos aserciones; errores agregados fieles a 472–498, corpus de 112 intacto. |
| 11, M-12 | `test_helper_json_publication_never_exposes_partial_target`, Jale publica ready/retries por replace atómico. Los padres salen con `os._exit`; no se simula muerte por señal. |
| 12, M-11 | Código/fixtures huérfanos eliminados; `test_owner_boot_allows_separator_but_ids_do_not`; sin reformateo global. |
| 13, M-9/M-10 | README: operator sin autoridad sobre otro supervisor; recuperación explícita F (SEN-3834/3835/3836). ProcessTable iguala boots y debe corregirse antes de integrar WSL. |
| 14, I-1/A-M3 | Registro fechado de tres corridas históricas y corrida r3 con SHA/recibo abajo. |
| Reconexión | F: no hay IPC/canal de reconexión autenticado ni supervisor productivo; el replay local no demuestra reconexión. |

Actualización de ejecución r3, 2026-10-23 (America/Mexico_City): run
`20261023-204639-a55097`, SHA probado `147ff527d04b6f622bce940ea27ed999520443e7`.
Árbol limpio y congelado durante espera/ejecución, Mini con turno y Python 3.9.6.
Una corrida, salida 0; no se usó la segunda. No hubo pruebas ni imports del candidato
en MacBook; sólo inspección AST de sintaxis 3.9 y diff.

| Familia | Descubiertas / ejecutadas | Resultado |
| --- | --- | --- |
| CLI | 9 | PASS |
| Contrato | 35 | PASS |
| Identidad/ámbito/U1 | 11 | PASS |
| Desk | 113 (112 originales + contraste) | PASS |
| Mini | 5 | PASS |
| Almacén | 15 | PASS |
| Total | 188 | 0 fallos / 0 errores / 0 omitidas |

A-I1: las dos pruebas nuevas dieron rojo sobre `dad5ef8` en esta misma corrida,
2 fallos y 0 errores de entorno: el dueño recibía exit 5 en lugar de 0 tanto en
reserva preparada como en latido. Dieron verde en el SHA probado; la secuencia
completa corregida incluye soltar con el mismo ID que el peer rechazado. B-G2 y
el rechazo durable autorizado también dieron verde. Fue rojo retrospectivo:
pruebas escritas antes del código, ejecución posterior en Mini con snapshot temporal.
El guion conserva las seis familias y exige exactamente 113 casos desk.

Evidencia completa, sin recortes:
`/Users/felixddhs/.claude/encargos/mini-carga/evidencia-hostgate-sol/495ef06ee60541bd8626124333b1b7f4/remoto.log`.
En el mismo directorio: `lanzador.log`, `script.sh`, `recibo.json` y `terminal.json`;
recibo completo, resultado y salida del lanzador 0, turno liberado. Los procesos
propios del lanzador terminaron; los helpers propios concluyeron en la suite.
El commit posterior cambia sólo README/COBERTURA, sin nueva ejecución. Esto acredita
suite verde sobre el SHA citado; la aceptación técnica de Fleet sigue pendiente,
al igual que las interfaces F. A-M1 se resuelve por adopción r4, registrada abajo.


Adopción A-M1, 2026-10-23 (America/Mexico_City): copia byte a byte de
`/Users/felixddhs/.claude/encargos/desktop-carga/lector-numero-am1/artifact/lector_desk.py`,
Desktop `d21312b1`, empaquetado en `ca44846d`. Blob verificado antes del commit:
`1ef0465d847836129dc155a80bf34fac8e169a4a`. Desktop acreditó en Mini los runs
`20261023-205755-d82822` (112/112 en base y corregido; regresiones 22 fallos en base
y 36/36 en corregido) y `20261023-210507-aedc03` (113/113 del corpus hostgate en ambos).
El corpus `test_lector_desk.py`, `support/cases.py` y `fixtures/health/desk.json`
conserva sus blobs. `test_lector_desk_adoptado.py` añade dos casos de JSON ±10**400
por el adaptador, con control sano, degradación y rechazo; se mantiene la captura
de desborde de `admission.py`. No queda pendiente A-M1 para Desktop. La evidencia
de ejecución de esta adopción se registra con la corrida r4.


Correcciones r4, 2026-10-23 (America/Mexico_City), pruebas escritas antes del código:

| Punto r4 | Prueba / límite |
| --- | --- |
| 1, autorización propia | `test_prepare_foreign_signal_is_not_an_os_error`; A-I1 y B-G2 intactos. |
| 2, A2-I1/N-3 | `test_a2_i1_foreign_rejection_persists_uncertainty_and_audit`, `test_a2_i1_foreign_reserve_persists_preparation_expiry`, `test_foreign_rejection_cannot_hide_persistence_failure`. |
| 3, N-2 | `test_n2_foreign_operations_cannot_consume_prepared_id`, `test_n2_owner_other_operation_does_not_consume_prepared_id`. |
| 4, A2-M1/N-1 | `test_internal_provider_errors_are_not_request_errors` incluye PermissionError; `test_symlink_os_permission_failure_is_store_unavailable`, `test_commit_and_ack_exceptions_always_replay_one_debit`. |
| 5, N-4 | Remisiones a tabla r4; replay compara el contexto antes de revelarlo; guion versionado `tests/mini-r4.sh`. |
| 6, adopción A-M1 | `DesktopAdoption.test_positive_huge_json_is_not_healthy`, `test_negative_huge_json_is_not_healthy`; corpus desk de 113 intacto, captura del adaptador conservada. |

El guion r4 adapta el de r3 dentro del subárbol autorizado; rojo retrospectivo
A2-I1/N-2 sobre `c23cc0f`, seis familias con 113 desk exactos. Los dos casos del
archivo nuevo se suman a contrato. Sólo tolera `?? .remote-run.lock` en porcelain.
Estado previo a la corrida: pruebas r4 escritas, aún no ejecutadas entonces. El registro r3 anterior
conserva run `20261023-204639-a55097`, SHA `147ff52`, 188/0/0 y rojo A-I1 sobre
`dad5ef8`, evidencia `495ef06ee60541bd8626124333b1b7f4/`. Suite verde sigue sin
equivaler a aceptación técnica de Fleet.


Ejecución r4, 2026-10-23 (America/Mexico_City): run `20261023-211240-fc0f10`,
SHA probado `1c3aef683a7df5df704fa5910be06a8f7d549a30`, incluida la adopción
Desktop `98a7bc1`. Árbol limpio y congelado durante espera y ejecución; sólo
se toleró la línea exacta de SEN-3885. Mini con turno, `/usr/bin/python3` 3.9.6,
macOS 27 arm64. Una corrida de dos permitidas; no se usó la segunda.

| Familia | Descubiertas / ejecutadas | Resultado |
| --- | --- | --- |
| CLI | 10 | PASS |
| Contrato + adaptador desk adoptado | 41 (39 + 2) | PASS |
| Identidad/ámbito/U1 | 11 | PASS |
| Desk | 113 (112 originales + contraste) | PASS |
| Mini | 5 | PASS |
| Almacén | 18 | PASS |
| Total | 198 | 0 fallos / 0 errores / 0 omitidas |

Rojo retrospectivo sobre `c23cc0fe6feda40b5383670554910011d303f4d2` en la
misma corrida: las cuatro pruebas A2-I1/N-2 fallaron, con 8 aserciones fallidas
(incluidos subtests), 0 errores. Se observaron `reserved` en vez de `uncertain`,
`validating` en vez de `expired`, consumo del id ajeno y latido del dueño en vez
de conflicto. Las cuatro pasaron en el SHA corregido. A-I1 de r3 y replay B-G2
permanecen intactos y pasaron. También pasaron PermissionError de proveedor y
symlink, commit/ACK/cierre con OSError/PermissionError/RuntimeError y recuperación
con un solo débito, además de JSON ±10**400 por el adaptador de salud.

Evidencia completa, sin recortes:
`/Users/felixddhs/.claude/encargos/mini-carga/evidencia-hostgate-sol/a746e9d92e33413eb68e93a22172e296/remoto.log`.
En ese directorio: `lanzador.log`, `script.sh`, `recibo.json` y `terminal.json`.
Recibo con `evidencia_completa=true`; resultado, start y salida del lanzador 0;
turno liberado. Lanzador terminado; helpers propios concluyeron en la suite.
No hubo pruebas ni imports del candidato en MacBook, conexiones al desk, push ni PR.
El guion se versionó dentro de `fleet/hostgate/` en vez de editar el externo
`evidencia-hostgate-sol/guion.sh`, para respetar el subárbol exclusivo autorizado.
La evidencia externa la escribió el runner autorizado. Después de la corrida
sólo cambian README y COBERTURA; el head documental no se afirma ejecutado.
Suite verde no equivale a aceptación técnica de Fleet ni activación. Siguen F
ABI/IPC, supervisor, perfiles reales, consumidor U1, corte de run.sh/mini.lock,
DefaultShell, SEN-3853, recuperación/reconciliación explícitas y medición del coste
con historial largo. A-M1 de Desktop ya está resuelto por la adopción autorizada.

SEN-3822 · corte 2b (2026-11-09), `tests/test_darwin_peer.py`:

| Diseño | Evidencia cubierta / límite |
| --- | --- |
| P07/P08 | Fuentes individuales divergentes, efectivos distintos de reales, errores y diez huecos en cada una de tres capturas; registro/reto/dispatch y PID con inicio/uniqueid/pidversion distinto. La misma implementación sirve a H←R, H←S y cliente←H. Barreras deterministas, sin PID wrap ni reboot real. |
| P09 | Conexiones propias después de exec, reto simétrico (entrada válida de 64 KiB), NOTE_EXIT, EOF y escritor heredado sin adopción del peer autenticado; hijos reapeados. EOF con dueño vivo también cierra el watch. |
| P13 / D1-W2 | Muestra generada por el productor real con sysctls fixture, lector Mini real y `Hostgate._health`; otro boot fresco, cada hueco del bracket UUID, boottime alterado sin UUID y fallos de lectura. Reserva previa conserva demanda/exclusivos y queda dudosa al cambiar UUID. |

R1-W1: `Health.test_observed_component_latches_before_following_failure` combina
UUID/boottime cambiado y fallo posterior en ambos extremos; restaura las fuentes
y exige salud cerrada con lector real e identidad cerrada si cambió UUID.

La comparación administrativa hardware UUID corresponde a 2a; saludo con
serviceUid/host/store/revisión, pairing, bindings de reservas y matriz IPC integrada
pertenecen a 2c. No se atribuye un ataque aceptado a main, que no tiene endpoint.

Ejecución Mini sobre `79bd8fcb7b86249b0a903260a6dc0dbd1bb14020`: dirigida
`20261109-130330-a263f5`, 17/17 por stack y comparación semántica de la nueva
representación frente a main. Suite única `20261109-130502-da97bc`: Apple 3.9.6,
227/227 (rc 0); Python 3.14.7, 226/227 (rc 1), sólo
`Storage.test_queries_never_mutate_sqlite_or_wal` por SEN-5625; cero errores/omitidas.
El commit posterior sólo registra esta evidencia; no se afirma ejecutado.

SEN-3822, 2c: `tests/test_ipc.py` añade procesos externos R/S/H, entorno distinto,
presupuesto conjunto de ejecutores, replay/digest, ACL de consulta, ofertas rechazadas,
segundo servicio, entrada máxima y límites, respuesta fragmentada e interrupciones.
P14 genera historial terminal mediante core/cierre sintético y compara el mapping
íntegro (>100 KiB), conservando inventario durable; SEN-5625 estaba declarado en aquel corte.
El saludo cliente contrasta uid/host/store/revisión/identidad antes de enviar intención.
La matriz nativa de 2b se repite dirigida; no acredita aún todas las ventanas
inyectadas a través del endpoint integrado ni PID wrap/reboot reales.
Mini `20261109-135305-fbd46e`, producto `75d52d11dedc02bf803bb20c4410131e55f5b668`:
34/34 por stack (Apple 3.9.6 y 3.14.7), P14 169.902 bytes sin cambio durable.
El head posterior sólo registra evidencia. Campaña integrada completa pendiente.

SEN-3822, **2c-I**, correcciones de la review de PR #67, ronda 1:

| Hallazgo | Regresión dirigida y aserción |
| --- | --- |
| R1-W1 | `IPC.test_r1_cancel_after_commit`: R/S/H y commit reales; cancelar tras DATA y antes de END exige TransportError/outcome_unknown, fd cerrado, una reserva y un slot retenido. |
| R1-W1 | `IPCQueryCancellation.test_r1_cli_cancel_before_end`: cancelación en connect/call y TransportError; exit 6, stderr y stdout vacío. El caso histórico inyectado sigue exigiendo 130. |
| R1-W1 | `ClientCancellation`: cancelación de open/connect/request_supervisor/approve cierra socket/watches o sesión y conserva outcome_unknown. Controles de handshake aislados, sin atribuirles un commit. |
| R1-W2 | `IPC.test_r1_query_only_second_executor`: configuración validada, catálogo other/unix y ACL sólo unix; consulta pública exit 0, control explícito unix exit 0. |
| R1-W2 | `IPC.test_r1_query_without_permitted_executor`: ACL vacía válida, rechazo explícito sin fallback al primer ejecutor. |

Las seis pruebas nuevas se suman a las 34 dirigidas anteriores. El guion conserva
TMPDIR/cache dentro de la copia y acorta la raíz temporal para los sockets nativos
Darwin. Evidencia rojo/verde por stack en el `evidence-2/report.md` del encargo.
La campaña integrada de autoridad y fallos sigue en **2c-II**; no se ejecuta aquí
la suite completa ni se habilitan cargas, Fleet efectivo o la Mini.

SEN-3822, **2c-II**, campaña por el endpoint y cliente productivos:

| Caso | Aserción nueva |
| --- | --- |
| CX-2, `Progress` | Entrada parcial, lector saturado con historial real y reto incompleto, cada uno detenido >=15 s; TTL=10 s, latidos <2 s, estado reserved, secuencia creciente, dueño/key/binding/debito/exclusivo conservados. Control sin Q y después del cierre; vencimiento real conserva duda. |
| Nativo, `Campaign` | H de R, H de S y cliente de H: fallos de una lectura, diez huecos por captura, tres capturas, registro/reto/dispatch y discrepancias entre fuentes. Cada negativo tiene control positivo, ledger/operations/audit sin cambios. Los huecos inyectan fallo de la siguiente lectura; no acreditan PID wrap ni reboot real. |
| Commit/canal | Cortes before_write/before_commit/after_commit y socket cortado después de DATA; replay original con un débito, outcome_unknown sin mapping parcial, canal nuevo del mismo PID y reinicio sin adopción. Fallo nativo durante revalidación no concede; cambio tras grant retiene. |
| Framing | END duplicado/tráfico sobrante no satisface otra llamada; JSON inválido/duplicado/no finito, contador extremo/overflow, ERROR, timeout y ENOSPC de escritura del spool cierran sin mapping. UTF-8 real y entradas de 65.535/65.536/65.537 bytes. |
| Ancla/ACL/P16 | B válido y B con storeId repetido rechazan sin cambiar inventario; reducción de A conserva key/exclusivos/débito y disponible negativo. UID/rol declarados y handoff alterado rechazan; cambios al segundo reservar conservan SQL operations/audit. |

Las seams de fallo son privadas del harness: ni disco lleno físico ni cuentas
adicionales, root, instalación, cargas o desk. Se conservan las regresiones de
pairing/replay/segunda instancia y vecinas de persistencia/contrato seleccionadas;
repetir 2b no se cuenta como evidencia nueva integrada. La suite completa queda
para el orquestador sobre el head con APTO. SEN-5625 estaba declarado en aquel corte.

SEN-5625, cierre del 2026-11-09:

Causa. La reproducción `20261109-170304-dd2ec6` sobre main
`e7244b5288740fd3389a258b5ef0ffe7c268b43e` da rc 0 con Apple Python 3.9.6 /
SQLite 3.54.0 y rc 1 con Python 3.14.7 / SQLite 3.53.4. La BD conserva sus 24.576
bytes y su SHA-256 tras diez consultas. Al cerrar el escritor, el SQLite de Apple
conserva un WAL de cero bytes y el de Homebrew lo elimina; la apertura de sólo
lectura lo vuelve a crear vacío. El test comparaba el conjunto de archivos y
fallaba por esa clave nueva.

Criterio. Una consulta muta cuando cambia contenido durable: los bytes de
`shadow.db`, los frames de `shadow.db-wal` o la vista lógica. Un WAL ausente y uno
de cero bytes son el mismo estado, y `shadow.db-shm` queda fuera.
`tests/support/durable.py` lo define una vez para Storage e IPC, que conserva además
tamaño y mtime.

Cobertura. `Storage.test_queries_never_mutate_sqlite_or_wal` parte de dos estados:
el que deja cada build al cerrar el escritor, y un WAL con frames que sostiene un
escritor abierto. En ambos exige inventario durable idéntico y la misma vista
lógica tras diez consultas. La sonda de la dirigida hace que la lectura escriba un
UPDATE con commit y exige el fallo por esa aserción en los dos stacks. Sus ids
constan en el PR.

No acredita. Apple SQLite 3.54.0 no abre `mode=ro` si faltan WAL y SHM
(`20261109-171155-e5a4ec`, también con una tabla mínima). Un store cerrado por el
build de Homebrew no se puede consultar con Apple Python: `estado` falla cerrado con
`StoreError` y no escribe. Queda en SEN-5678. Los ResourceWarning de la suite nacen
en conexiones de `tests/test_store.py` abiertas con `with sqlite3.connect(...)`, que
no cierra; no participan en este fallo y no se corrigen aquí.


SEN-5649, entrega **3a**, reserva efectiva preparatoria:

`test_effective_hostgate.py` usa el endpoint real con R/S/H nativos privados y un
grant efectivo previo a cada negativo. Cubre ledger vacío/generación 1, wire sin
simulación, replay con un débito, rechazo de presupuesto, copia/rename/metadata de
sombra, directorio existente, cambio de modo sin ancla, cliente/hello discordantes,
keys viejas, `soltar` con fixture que retiene, latido por el canal dueño, validación
de estados futuros y recibos corruptos, y plazo acotado del cliente.
`tests/support/effective.py` mantiene un escritor vivo y WAL/SHM para las lecturas
`mode=ro` de Apple (SEN-5678); no salta ese límite ni acredita un store sin sidecars.
Los ids de las corridas en la Mini (rojo en la base, mutantes y verde dirigido, en
Apple 3.9.6 y Homebrew 3.14.7) constan en el PR. En la base las pruebas no fallan
por aserción: el producto rechaza `mode: effective` al cargar la configuración.
Las lecturas de inventario fijan también un read-mark vivo: comparan todos los
archivos, incluido SHM, sin debilitar la aserción ni saltar el límite de Apple.
No acredita perfiles/creación/cierre reales, provisión, integración Fleet ni activación.


SEN-5650, entrega **4a**, lector de coalición, `tests/test_darwin_coalition.py`:

| Grupo | Cobertura y límite |
| --- | --- |
| `Rules`, kernel sustituido (`tests/support/coalition.py`) | Una prueba por regla con su positivo: `Complete` sin novedad; `Occupied` con un nacimiento nuevo, un hijo de un miembro ajeno de la línea base, un pid reutilizado con otro `uniqueid` y miembros elevados; `Retry` por contadores que cambian (C12 con el recuento vivo igual, y sólo `tasks_exited`), cardinalidad, lista saturada que omite un hijo vivo, identidad cambiante y tope de 50 ms; `Invalid` por boot, coalición movida o nula, contadores incoherentes, tamaño inesperado y errores distintos de `ESRCH`. Zombi con y sin concordancia. Línea base alterada de diecinueve formas y por encima de 256 miembros. `observe_root` con nieto, otra coalición, zombi, identidad y boot cambiantes. |
| `SharedCoalition`, kernel real en `remote-run` | Línea base íntegra con la prueba como miembro; hijos por `fork`, `posix_spawn` y `fork`+`exec` y un nieto con doble `fork`, `setsid` y `exec` sin entorno aparecen con su identidad exacta y desaparecen al terminar; C12 con barrera da `Retry`; un zombi propio listado no cuenta; `observe_root` acepta el hijo directo y rechaza nieto y pid 1; `posix_spawnattr_setcoalition_np` da `EPERM` (K6). Cada novedad se clasifica contra lo creado por la prueba. No afirma `Complete`. |
| `OwnCoalition`, sólo con `HOSTGATE_OWN_COALITION=1` | Desde una conexión ssh no multiplexada: shell con hijo y nieto huérfano da `Occupied` exacto y, al terminar, `Complete`. Corre fuera de `remote-run`; su resultado consta en el PR. |

Variantes defectuosas que cada prueba de `Rules` mata por aserción:
`compare_live_count_only`, `compare_started_only`, `esrch_means_empty`,
`omit_new_birth`, `refresh_baseline`, `subtract_foreign_descendants`,
`ignore_truncation`, `skip_member_count`, `unknown_abi_is_empty` y
`pid_only_identity`. Los ids de la corrida en la Mini constan en el PR.
No acredita IPC hacia servicios, privilegios, otra coalición ni builds futuros.


SEN-5650, entrega **4b-1**, formas del permiso y evidencia real:

| Prueba | Cobertura y límite |
| --- | --- |
| `test_creation_forms.py`, `Forms` | Una fila efectiva por combinación legal de estado y permiso, escrita y releída por el store, y la tabla completa de estado por variante del permiso, donde cada combinación ilegal se rechaza. Vecinas ilegales con un solo cambio: `running` sin raíz, línea base o recibo; raíz dentro de la línea base, de otra coalición o con pid o uid fuera de rango; `released` sin evidencia, con la de otra reserva, con una prueba a la que falta un campo o con un permiso que no es `Closed` ni `Sealed`; `Sealed` con raíz; `Consumed` con `launchPossible` falso; un campo del permiso alterado (digests, contadores, epoch, coalición, línea base, dueño o canal); campo de más en cada variante. `simulation` en efectivo retiene. La evidencia real re-firmada tras alterar un solo campo retiene campo por campo: key (también su tipo), store, boot, epoch, proveedor, perfil, permiso, confirmación, lanzamiento, coalición, línea base, raíz, política, base terminal, digest de supervivientes, cada barrido (tipo, longitud, orden, igualdad y anterioridad a la línea base) y cada cuenta (tipo, igualdad entre sí y con el barrido, cota de la línea base). También retienen `never-created` con un campo de barrido no nulo o con permiso consumido, y un permiso de otro dueño o sin fondo; `creation_from_row` rechaza el de otro boot. Una fila ilegible retiene en los dos modos y no lanza. La correlacionada no deja motivos. La evidencia del fixture efectivo no libera: `soltar` responde `enforcement-unavailable` y la reserva queda `uncertain`. |
| `test_creation_forms.py`, `ShadowForms` | En sombra, una fila con permiso real, con evidencia real, con `simulation` que lleva observación o con emisor sin texto es ilegal; la de hoy sigue valiendo y `soltar` escribe la misma forma que antes. |

Variantes defectuosas que las pruebas matan por aserción: `mode_from_prefix`,
`accept_synthetic`, `trust_supervisor_clean`, `running_without_root`,
`sealed_with_root`, `released_without_proof`, `real_permit_in_shadow`,
`root_in_baseline`, `foreign_epoch`, `any_provider`, `scan_before_baseline`,
`shadow_writes_observation`, `survivors_exceed_baseline`, `loose_key`,
`loose_scan_after`, `permit_owner_unbound`, `extra_field_consumed`, `cardinality_unchecked`, las cuatro `unreadable_*_escapes` y, sólo en
Python 3.9, `recursion_escapes`. En un triaje con una variante por cláusula de `scope.py`, ninguna acepta una entrada de su familia perturbada que el producto rechace sin que falle una prueba de hostgate. 56 variantes no tienen testigo en esa familia, y eso no acredita que sean redundantes fuera de ella. Los ids de las corridas en la Mini constan en el PR. No acredita la
emisión ni el consumo del permiso, el supervisor ni el cierre real.


SEN-5650, entrega **4b-2**, acreditación, store v2 y selfcheck:

| Prueba | Cobertura y límite |
| --- | --- |
| `test_creation_forms.py`, `Store` | Una vista ilegal no se escribe y, plantada a mano, no carga. Un store efectivo v1 creado en la prueba falla con `schema` en lectura y escritura y conserva inodo, mtime y bytes sin nombres nuevos (el SQLite de Apple ya deja `-wal`/`-shm` desde `initialize`). |
| `test_profile_accreditation.py` | Configuración de hoy con su `content_digest`; bloque estricto (de más, de menos, tipos), congelado y en la revisión; sin bloque, build o uid distintos, selfcheck fallido o ausente dan `profile-unverified` con su detalle; el nombre del perfil no acredita. |
| `test_coalition_selfcheck.py` | Kernel real en `remote-run`: el selfcheck pasa con el boot y el build observados, también si la raíz o el huérfano cambian una vez entre lecturas. Con el acceso nativo envuelto, tamaño inesperado, error de lectura, huérfano ausente, zombi legible como vivo, proceso propio que sigue vivo, `exec` fallido, acceso nativo que no se construye, una espera fallida en la limpieza y un huérfano detenido con la espera del intermedio interrumpida y otra identidad ilegible al revalidar dan `SelfcheckFailed` con paso y motivo, sin dejar identidades propias vivas. Nada llama a `accredit` ni al selfcheck. |

Variantes defectuosas que las pruebas matan por aserción: `profile_id_is_certificate`,
`reset_store`, `mutate_unvalidated`, `selfcheck_ignores_failure`,
`exec_status_ignored`, `native_init_escapes`, `cleanup_stops_on_failure`,
`orphan_registered_late`, `revalidate_in_batch` y `root_not_retried`. Los ids de las
corridas en la Mini constan en el PR. No acredita la conexión de la acreditación ni
del selfcheck al runtime.
