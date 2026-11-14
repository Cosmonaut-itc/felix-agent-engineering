#!/usr/bin/python3 -I
"""Mutaciones de U3.1 (lugares por grupo) en copias temporales de fleet/rack y fleet/pueue-desk.

Una por regla de seguridad nueva. Cada una debe hacer FALLAR (FAIL, no ERROR) la prueba
nombrada. Sólo usa dobles: TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -I tests/mutate_slots.py
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

FLEET = Path(__file__).resolve().parents[2]
MINI = ("pueue-desk", "test_slots_mini")
INSTALL = ("pueue-desk", "test_pueue_mini")
RACK = ("rack", "test_slots_rack")
# Regla, archivo relativo a fleet/, fragmento original, mutación, (carpeta, módulo), prueba.
MUTATIONS = [
    ("tope validado en el host", "pueue-desk/control.py", '    return "capacity"\n', '    return None\n',
     MINI, "MutateTests.test_apply_down_up_and_host_side_cap"),
    ("sobre el tope sólo reducción monotónica", "pueue-desk/control.py",
     "if previous > cap and total < previous and all(target[g] <= before[g] for g in target):",
     "if previous > cap and total <= previous:", MINI, "RequestTests.test_check_slots_bounds_cap_and_monotonic_reduction"),
    ("mínimo 1 en el host", "pueue-desk/control.py", "if value < SLOT_MIN:", "if value < 0:",
     MINI, "RequestTests.test_check_slots_bounds_cap_and_monotonic_reduction"),
    ("máximo 8 en el host", "pueue-desk/control.py", "if value > SLOT_MAX:", "if value > 9:",
     MINI, "RequestTests.test_check_slots_bounds_cap_and_monotonic_reduction"),
    ("delta entero, no booleano", "pueue-desk/control.py", 'or change["group"] not in MINI_PROFILE or type(change["delta"]) is not int',
     'or change["group"] not in MINI_PROFILE or not isinstance(change["delta"], int)',
     MINI, "RequestTests.test_closed_schema_rejects_before_any_rpc"),
    ("reducciones antes que aumentos", "pueue-desk/control.py",
     "    for group in sorted(observed):\n        if exceeds(observed[group], target[group]):",
     "    for group in sorted(observed):\n        if observed[group] != target[group]:",
     MINI, "MutateTests.test_reductions_before_increases_and_unconfirmed_reduction_stops"),
    ("bajada confirmada antes de subir", "pueue-desk/control.py",
     '        raise ValueError(f"Reducción sin confirmar: {observed}")', "        pass",
     MINI, "MutateTests.test_reductions_before_increases_and_unconfirmed_reduction_stops"),
    ("0 en parallel es ilimitado", "pueue-desk/control.py", "return observed == 0 or observed > limit",
     "return observed > limit", MINI, "MutateTests.test_reductions_before_increases_and_unconfirmed_reduction_stops"),
    ("el ensayo se bifurca antes de escribir", "pueue-desk/control.py",
     '        if dry:\n            return reply(request, "dry_run", None, view["revision"], result)\n', "",
     MINI, "MutateTests.test_dry_run_plans_without_effects"),
    ("el ensayo no crea control.lock", "pueue-desk/control.py", "with host_lock(directory, create=not dry)",
     "with host_lock(directory, create=True)", MINI, "MutateTests.test_dry_run_plans_without_effects"),
    ("mismo ID, mismo recibo", "pueue-desk/control.py",
     '            row = store.action(request["action_id"])\n            if row is not None:',
     '            row = store.action(request["action_id"])\n            if False:',
     MINI, "MutateTests.test_same_id_same_receipt_other_body_conflict"),
    ("estado visto obligatorio", "pueue-desk/control.py",
     'if (expected["revision"] != view["revision"] or ', "if (", MINI, "MutateTests.test_stale_drift_and_busy"),
    ("un cambio abierto bloquea", "pueue-desk/control.py", "        if store.open_action():", "        if False:",
     MINI, "MutateTests.test_stale_drift_and_busy"),
    ("deshacer sólo en su revisión", "pueue-desk/control.py",
     ' or original["revision"] != view["revision"]', "", MINI, "MutateTests.test_undo_is_exact_and_single"),
    ("no se deshace un deshacer", "pueue-desk/control.py", 'original["op"] != "slots" or ', "",
     MINI, "MutateTests.test_undo_is_exact_and_single"),
    ("la guardia repone el deseo", "pueue-desk/control.py",
     "        if any(observed[g] != desired[g] for g in observed):\n            before = observed",
     "        if False:\n            before = observed", MINI, "GuardTests.test_lost_parallel_is_reconciled_before_opening"),
    ("la guardia cierra recibos", "pueue-desk/control.py",
     "            for action_id in store.close_open(observed):", "            for action_id in ():",
     MINI, "FailureTests.test_failure_and_crash_after_each_rpc"),
    ("abrir sólo reconciliado", "pueue-desk/control.py",
     'status == "Paused" and can_open:', 'status == "Paused":', MINI, "GuardTests.test_unreconciled_or_unreadable_never_opens"),
    ("sin autoridad legible no se abre", "pueue-desk/control.py",
     "            can_open, failure = False, error\n", "            failure = error\n",
     MINI, "GuardTests.test_unreconciled_or_unreadable_never_opens"),
    ("propiedad antes de la pausa", "pueue-desk/control.py",
     '            owned.add(group)\n            save_owned(directory, owned)\n            pueue.call("pause", "--wait", "-g", group)',
     '            pueue.call("pause", "--wait", "-g", group)\n            owned.add(group)\n            save_owned(directory, owned)',
     MINI, "GuardTests.test_ownership_is_written_before_pause"),
    ("adopción sólo del perfil", "pueue-desk/control.py", "    if observed != MINI_PROFILE:\n", "    if False:\n",
     MINI, "MutateTests.test_adopt_requires_profile_and_never_overwrites"),
    ("el instalador exige el perfil antes de cambiar", "pueue-desk/install-mini.py",
     "    elif control.observed_slots(status) != control.MINI_PROFILE:", "    elif False:",
     INSTALL, "InstallerTests.test_first_install_with_other_quotas_aborts_before_changes"),
    ("el canal rack acota Pueue", "pueue-desk/control.py", "        pueue.timeout = RACK_PUEUE_TIMEOUT\n", "",
     MINI, "CliTests.test_rack_cli_reads_stdin_outside_the_guard_lock"),
    ("panel: instantánea vieja no envía", "rack/accion.py",
     "if (not current or not current['fresh'] or current.get('age') is None or current['age'] > 45\n"
     "                or self.clock() - payload['now'] > 45):\n            return 'stale_state'",
     "if not current:\n            return 'stale_state'", RACK, "FlowTests.test_rejections_bind_the_id_before_any_transport"),
    ("panel: ensayo sin diario", "rack/accion.py", "        if request['dry_run']:\n            # Dry run",
     "        if False:\n            # Dry run", RACK, "FlowTests.test_dry_run_has_no_journal_row_and_no_effect"),
    ("panel: un cambio por host", "rack/accion.py", "else 'busy' if self.host_pending(db, request)",
     "else 'busy' if False", RACK, "FlowTests.test_rejections_bind_the_id_before_any_transport"),
    ("panel: unknown se resuelve con el mismo cuerpo", "rack/accion.py",
     "                            return host_result(request, 'unknown')\n                        continue",
     "                            return host_result(request, 'unknown')\n                        return host_result(request, 'unknown')",
     RACK, "FlowTests.test_unknown_get_resends_same_body_once_and_applies_once"),
    ("panel: ± sólo con control limpio", "rack/accion.py",
     "slots = caps['slots'] and control['reason'] is None and None not in (revision, digest, desired, observed)",
     "slots = caps['slots']", RACK, "ContractTests.test_host_view_and_sanitize_whitelist"),
    ("panel: resultado del host en lista blanca", "rack/accion.py",
     "not set(value) <= {'before', 'desired', 'observed', 'plan', 'undo_of'}", "False",
     RACK, "ContractTests.test_host_view_and_sanitize_whitelist"),
    ("UI: tope lleno deshabilita +", "rack/index.html",
     "if (delta > 0 && Object.values(c.desired).reduce((a,b)=>a+b,0) >= c.cap)", "if (false)",
     RACK, "UITests.test_slots_ui_states"),
    ("UI: un cambio en curso deshabilita", "rack/index.html", "if (SLOTS.has(h.id) || c.pending)", "if (false)",
     RACK, "UITests.test_slots_ui_states"),
    ("UI: 404 reenvía una sola vez", "rack/index.html",
     "data.action_id===action.request.action_id && !action.resent){\n      action.resent=true; persistSlots();",
     "data.action_id===action.request.action_id){\n      action.resent=true; persistSlots();",
     RACK, "UITests.test_slots_ui_states"),
]


def main():
    killed = 0
    for rule, relative, original, replacement, (folder, module), test in MUTATIONS:
        with tempfile.TemporaryDirectory(prefix="u3-1-mutacion-", dir="/tmp") as temporary:
            copy = Path(temporary) / "fleet"
            for name in ("rack", "pueue-desk"):
                shutil.copytree(FLEET / name, copy / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            target = copy / relative
            source = target.read_text()
            if source.count(original) != 1:
                raise ValueError(f"Mutación ambigua o caduca: {rule}")
            target.write_text(source.replace(original, replacement))
            command = ["/usr/bin/python3", "-B", "-m", "unittest", f"{module}.{test}", "-v"]
            result = subprocess.run(command, cwd=str(copy / folder / "tests"), text=True, capture_output=True,
                                    env=dict(os.environ, TMPDIR="/tmp", PYTHONDONTWRITEBYTECODE="1"), timeout=120)
            output = result.stdout + result.stderr
            name = test.split(".")[-1]
            if result.returncode == 0 or f"FAIL: {name}" not in output:
                print(output, flush=True)
                raise AssertionError(f"Mutación sobrevivió o no falló por aserción: {rule}")
            killed += 1
            evidence = [line for line in output.splitlines() if line.startswith("AssertionError")]
            print(f"RECHAZADA {killed:02}: {rule} | {test} | {(evidence or ['?'])[0][:150]}", flush=True)
    print(f"Resultado: {killed}/{len(MUTATIONS)} mutaciones rechazadas por aserción", flush=True)


if __name__ == "__main__":
    main()
