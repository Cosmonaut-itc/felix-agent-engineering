#!/usr/bin/python3 -I
"""Mutaciones dirigidas en copias temporales; ejecutar exclusivamente en la Mini."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
# Regla, archivo, fragmento original, mutación, prueba que debe rechazarla.
MUTATIONS = [
    ("pausa por carga estricta >2", "control.py", 'readings["load"] > high["load"]',
     'readings["load"] >= high["load"]', "test_load_hysteresis_and_admission_only"),
    ("reanuda con carga estricta <1.2", "control.py", 'readings["load"] < low["load"]',
     'readings["load"] <= low["load"]', "test_load_hysteresis_and_admission_only"),
    ("memoria aviso 2 pausa", "control.py", 'readings["memory"] >= high["memory"]',
     'readings["memory"] > high["memory"]', "test_memory_warning_and_critical_pause"),
    ("carga O memoria alta", "control.py", 'readings["load"] > high["load"] or',
     'readings["load"] > high["load"] and', "test_memory_warning_and_critical_pause"),
    ("carga baja Y memoria normal", "control.py", 'readings["load"] < low["load"] and',
     'readings["load"] < low["load"] or', "test_load_hysteresis_and_admission_only"),
    ("no reclama pausas manuales", "control.py", 'action == "pausar" and status == "Running"',
     'action == "pausar" and status in ("Running", "Paused")', "test_respects_manual_pause"),
    ("relevo manual retira propiedad", "control.py", '                owned.discard(args.group)',
     '                pass  # mutación: conserva propiedad', "test_manual_takeover_and_cli_profile"),
    ("arranque externo retira propiedad", "control.py", 'if group in owned and status != "Paused":',
     'if False:', "test_external_start_drops_ownership"),
    ("start fallido conserva propiedad", "control.py", '            pueue.call("start", "-g", group)\n            owned.remove(group)\n            save_owned(directory, owned)',
     '            owned.remove(group)\n            save_owned(directory, owned)\n            pueue.call("start", "-g", group)', "test_failed_resume_keeps_ownership"),
    ("carga negativa inválida", "control.py", 'load < 0', 'False',
     "test_invalid_readings_do_not_decide_or_change_ownership"),
    ("carga no finita inválida", "control.py", 'not math.isfinite(load)', 'False',
     "test_invalid_readings_do_not_decide_or_change_ownership"),
    ("nivel desconocido inválido", "control.py", 'level not in (1, 2, 4)', 'False',
     "test_invalid_readings_do_not_decide_or_change_ownership"),
    ("núcleos enteros positivos", "control.py", 'type(cores) is not int', 'False',
     "test_invalid_readings_do_not_decide_or_change_ownership"),
    ("error se registra", "control.py", '            log(directory, "error", detalle=str(error))',
     '            pass', "test_cli_invalid_reading_logs_error_and_exits_nonzero"),
    ("error sale distinto de cero", "control.py", '            log(directory, "error", detalle=str(error))\n            raise\n',
     '            log(directory, "error", detalle=str(error))\n            return\n', "test_cli_invalid_reading_logs_error_and_exits_nonzero"),
    ("CLI Mini usa binario Homebrew", "control.py", 'Pueue("/opt/homebrew/bin/pueue")',
     'Pueue("/usr/bin/pueue")', "test_manual_takeover_and_cli_profile"),
    ("CLI comparte candado", "control.py", '    with locked(directory):',
     '    if True:', "test_manual_takeover_and_cli_profile"),
    ("Mini no aplica cuotas", "control.py", 'if args.host == "mini" and args.command == "aplicar":',
     'if False:', "test_mini_cannot_apply_quotas"),
    ("pause sólo admisión --wait", "control.py", 'pueue.call("pause", "--wait", "-g", group)',
     'pueue.call("pause", "-g", group)', "test_load_hysteresis_and_admission_only"),
    ("--seco no hace acciones", "install-mini.py", '    if args.seco:',
     '    if False:', "test_dry_run_has_no_actions"),
    ("exige sh.brew.pueue", "install-mini.py", '        run(LAUNCHCTL, "print", f"{domain}/sh.brew.pueue")',
     '        pass', "test_missing_brew_or_unresponsive_pueue_aborts_before_changes"),
    ("exige respuesta Pueue", "install-mini.py", 'run(PUEUE, "status", "--json").stdout',
     'run(PUEUE, "status", "--json", check=False).stdout', "test_missing_brew_or_unresponsive_pueue_aborts_before_changes"),
    ("valida grupos y estado", "install-mini.py", 'if not isinstance(status.get("tasks"), dict) or not {"suites", "default"} <= status["groups"].keys():',
     'if False:', "test_invalid_status_aborts_before_changes"),
    ("reinstala idempotente", "install-mini.py", 'if run(LAUNCHCTL, "print", service, check=False).returncode == 0:',
     'if False:', "test_install_twice_plist_and_withdraw_twice_resumes_owned_groups"),
    ("retirada reanuda grupos propios", "install-mini.py", '            run(PUEUE, "start", "-g", group)\n',
     '', "test_install_twice_plist_and_withdraw_twice_resumes_owned_groups"),
    ("retirada vacía la propiedad tras reanudar", "install-mini.py", "- {group})", ")",
     "test_install_twice_plist_and_withdraw_twice_resumes_owned_groups"),
    ("StartInterval 60", "launchagents/local.pao.pueue-mini.guard.plist", '<integer>60</integer>',
     '<integer>61</integer>', "test_install_twice_plist_and_withdraw_twice_resumes_owned_groups"),
    ("sin KeepAlive", "launchagents/local.pao.pueue-mini.guard.plist", '    <key>StartInterval</key>',
     '    <key>KeepAlive</key><true/>\n    <key>StartInterval</key>', "test_install_twice_plist_and_withdraw_twice_resumes_owned_groups"),
    ("Python aislado -I", "launchagents/local.pao.pueue-mini.guard.plist", '        <string>-I</string>\n',
     '', "test_install_twice_plist_and_withdraw_twice_resumes_owned_groups"),
    ("instalación fallida reanuda grupos propios", "install-mini.py", '        except Exception:\n            resume_owned(state)\n            raise\n',
     '        except Exception:\n            raise\n', "test_failed_bootstrap_resumes_owned_groups"),
    ("retirada fallida recarga la guardia", "install-mini.py", '            run(LAUNCHCTL, "bootstrap", domain, str(agent), check=False)\n',
     '', "test_failed_resume_on_withdraw_reloads_guard_with_ownership"),
    ("desk conserva umbral RAM 4GiB", "control.py", '"memory": 4 * 1024**3',
     '"memory": 3 * 1024**3', "test_thresholds"),
    ("log conserva tope", "control.py", 'if target.exists() and target.stat().st_size >= LOG_MAX_BYTES:',
     'if False:', "test_rotation"),
]


def main():
    killed = 0
    for rule, relative, original, replacement, test in MUTATIONS:
        with tempfile.TemporaryDirectory(prefix="u18-mutacion-") as temporary:
            copy = Path(temporary) / "pueue-desk"
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns("__pycache__"))
            target = copy / relative
            source = target.read_text()
            if source.count(original) != 1:
                raise ValueError(f"Mutación ambigua o caduca: {rule}")
            target.write_text(source.replace(original, replacement))
            pattern = "test_pueue_desk.py" if test in ("test_thresholds", "test_rotation") else "test_pueue_mini.py"
            command = ["/usr/bin/python3", "-I", "-m", "unittest", "discover", "-s", str(copy / "tests"),
                       "-p", pattern, "-k", test, "-v"]
            result = subprocess.run(command, text=True, capture_output=True)
            output = result.stdout + result.stderr
            rejected = result.returncode != 0 and ("FAIL: " + test in output or "ERROR: " + test in output)
            if not rejected:
                print(output, flush=True)
                raise AssertionError(f"Mutación sobrevivió o no ejecutó su prueba: {rule}")
            killed += 1
            evidence = [line for line in output.splitlines() if line.startswith(("FAIL:", "ERROR:", "AssertionError:"))]
            print(f"RECHAZADA {killed:02}: {rule} | {test} | {'; '.join(evidence[:2])}", flush=True)
    print(f"Resultado: {killed}/{len(MUTATIONS)} mutaciones rechazadas", flush=True)


if __name__ == "__main__":
    main()
