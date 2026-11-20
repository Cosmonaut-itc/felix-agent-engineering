import json
from pathlib import Path
import sys
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pool import FAKE_ID, NAME, NOW, Harness, pool


def summarize(got, expected):
    print(f"  actual:   {got}")
    print(f"  esperado: {expected}")


def retention(harness):
    for archive_after in (None, pool.stamp(NOW + 7 * 86400)):
        harness.api.sandbox(
            FAKE_ID, state="archived",
            archiveAfter=archive_after,
            stoppedAt=pool.stamp(NOW - 13 * 86400),
        )
        harness.api.calls.clear()
        harness.notices.clear()
        code, _, _ = harness.run_cli("vigilar")
        print(f"A1 `vigilar`, VM detenida hace 13 días, archiveAfter={archive_after}")
        renewals = [f"{method} {path}" for method, path, _ in harness.writes()]
        summarize(
            f"rc={code}; renovaciones={renewals}; avisos={harness.notices}",
            "retención del disco renovada, o aviso verificable si el intento falla; "
            "un TTL de autoapagado futuro no acredita la retención del disco",
        )


def daily_org(harness):
    code, output, _ = harness.run_cli("reporte", "--dia", "2026-11-23")
    print("A2 `reporte --dia 2026-11-23` con credencial y transporte inyectados")
    summarize(
        f"rc={code}; TSV={output.strip()!r}; consultas={harness.api.calls}",
        "consultar la fuente de Boat y presentar totales diarios de toda la org, "
        "o acreditar por qué la API no permite recuperar esos totales",
    )


def billable_provisioning(harness):
    harness.api.sandbox(FAKE_ID, state="archived")

    def advance(_):
        current = pool.read_json(harness.api.path)[FAKE_ID]["state"]
        if current == "provisioning":
            harness.api.sandbox(FAKE_ID, state="provisioned")
        elif current == "provisioned":
            harness.api.now += 60
            harness.api.sandbox(FAKE_ID, state="ready")
        else:
            raise AssertionError(current)

    with patch.object(harness.api, "sleep", advance):
        power_code, _, power_error = harness.run_cli("encender", NAME)
    assert power_code == 0, power_error
    code, output, _ = harness.run_cli("reporte", "--dia", "2026-11-23")
    print("A3 `encender` y `reporte --dia 2026-11-23`, large en provisioned 60 s antes de ready")
    summarize(
        f"rc={code}; TSV={output.strip()!r}",
        "2026-11-23\t0.000000\t0.016667\t0.000000\t0.001200\t1\t0\tNA\tNA",
    )


def prepare_queue(harness):
    lease = harness.get_lease("preparar-activa", "preparar", "--vm", NAME)
    record_path = harness.state / "vms" / NAME / "registro.json"
    wrote = False

    def finish_prepare(_):
        nonlocal wrote
        assert not wrote, "la siguiente iteración debía tomar la VM recién preparada"
        assert len(list((harness.state / "espera").glob("*.json"))) == 1
        record = pool.read_json(record_path)
        record.update(preparada_en=pool.stamp(NOW + 1), revision_runner="rev-nueva")
        pool.atomic_json(record_path, record)
        harness.api.now = NOW + 1
        code, _, error = harness.run_cli("soltar", lease)
        assert code == 0, error
        wrote = True

    with patch.object(harness.api, "sleep", finish_prepare):
        code, output, error = harness.run_cli(
            *harness.args("suite-en-fila", "contract", "--esperar", "5")
        )
    assert wrote
    next_code, next_output, _ = harness.run_cli(*harness.args("suite-reintento"))
    assert next_code == 0
    print("A4 `pedir --esperar 5`, seguido del registro atómico y `soltar` de preparar")
    summarize(
        f"espera: rc={code}, stdout={output.strip()!r}, stderr={error.strip()!r}; "
        f"reintento: rc={next_code}, vm={json.loads(next_output)['vm']}",
        "la solicitud en fila obtiene la VM liberada con rc=0 sin reintento",
    )


def gates_fail_closed(harness):
    harness.api.sandbox(FAKE_ID, state="archived")
    for args in (("encender", NAME), harness.args()):
        harness.api.calls.clear()

        def unavailable(method, path, headers, body, timeout):
            if path == "/limits":
                raise TimeoutError("falla simulada")
            return transport(method, path, headers, body, timeout)

        transport = harness.api.transport
        with patch.object(harness.api, "transport", unavailable):
            code, _, error = harness.run_cli(*args)
        assert code == 75 and not harness.writes(), (args, code, error, harness.writes())
        print(f"Control `{args[0]}`, /limits caído: rc={code}; mutaciones={harness.writes()}")


if __name__ == "__main__":
    for case in (retention, daily_org, billable_provisioning, prepare_queue, gates_fail_closed):
        fixture = Harness()
        fixture.setUp()
        try:
            case(fixture)
        finally:
            fixture.doCleanups()
